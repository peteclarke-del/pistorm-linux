"""Lifting the Amiga drives out of a card image as separate ``.hdf`` files.

The tool could already write its *output* as a bare ``.hdf`` instead of a card,
and that only ever described one drive. A PiStorm card normally carries four -
a system drive, games, demos, a work drive - and a single bare file cannot say
which of them it is, so the option was quietly wrong for every card this tool
actually builds. Reading the drives back out is a different job and gets its
own one.

Each drive is written **self-contained**: its own Rigid Disk Block naming it,
with the file system handler the source card embedded copied in beside it, so
WinUAE and FS-UAE mount the file with nothing else supplied. That matters most
for PFS3, which no emulator has built in - a bare copy of those blocks is
unreadable without the handler, and the volume name is lost with it.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import BinaryIO, Iterable

from . import rdb
from .util import Progress, human_size

SECTOR = 512
#  Read and write in chunks rather than a drive at a time: a games drive is
#  twenty gigabytes and does not belong in memory.
CHUNK = 8 * 1024 * 1024


@dataclasses.dataclass(frozen=True)
class Drive:
    """One Amiga drive found inside an image, as it would be exported."""

    name: str                   # the device name in the RDB - DH0, DH1
    dostype: int
    start: int                  # byte offset within the image
    size: int                   # bytes
    volume: str = ""            # the name AmigaDOS shows, when it can be read
    bootable: bool = False

    @property
    def label(self) -> str:
        """What to call it on screen: the volume name if it has one."""
        return self.volume or self.name

    @property
    def description(self) -> str:
        kind = rdb.dostype_name(self.dostype)
        volume = f' "{self.volume}"' if self.volume else ""
        return f"{self.name}  {kind}  {human_size(self.size)}{volume}"

    def filename(self) -> str:
        """A file name for this drive, safe on any host file system."""
        stem = "".join(c if c.isalnum() or c in "-_" else "_"
                       for c in self.label).strip("_") or self.name
        return f"{stem}.hdf"


def _volume_name(handle: BinaryIO, start: int, dostype: int) -> str:
    """The name AmigaDOS shows for a drive, or "" when it cannot be read.

    Best effort by design. A drive this tool cannot read is still a drive
    worth exporting - the point of the export is to hand it to something that
    can - so failing to name it must never be the reason it is left out.
    """
    try:
        from . import amigafs, pfs3                          # noqa: PLC0415
        if dostype == rdb.parse_dostype("PFS3"):
            return pfs3.Pfs3Volume(handle, start).name or ""
        return amigafs.Volume(handle, start).name or ""
    except Exception:                                        # noqa: BLE001
        return ""


def drives(path: str | Path) -> list[Drive]:
    """Every Amiga drive in an image, whether it is a card or a bare ``.hdf``.

    Both shapes are accepted for the same reason the rest of this tool accepts
    them: somebody exporting drives has whatever they were given.
    """
    from . import builder                                    # noqa: PLC0415

    path = Path(path)
    found: list[Drive] = []
    with open(path, "rb") as handle:
        located = builder.find_rdb(handle)
        if located is None:
            return []
        base, table = located
        cyl = table.geometry.cyl_blocks * table.geometry.block_size
        for part in table.partitions:
            start = base + part.low_cyl * cyl
            size = (part.high_cyl - part.low_cyl + 1) * cyl
            found.append(Drive(
                name=part.drive_name, dostype=part.dostype,
                start=start, size=size, bootable=part.bootable,
                volume=_volume_name(handle, start, part.dostype)))
    return found


def _table_for(drive: Drive, source: rdb.Rdb) -> rdb.Rdb:
    """A Rigid Disk Block describing this one drive, on its own.

    The handler travels with it. Without that an emulator is handed a PFS3
    drive and no way to read it, which is the failure this whole feature
    exists to avoid.
    """
    geometry = source.geometry
    reserved = 2016
    total_blocks = reserved + drive.size // geometry.block_size
    cylinders = -(-total_blocks // geometry.cyl_blocks)
    parts = rdb.layout(geometry, cylinders * geometry.cyl_blocks,
                       [(drive.name, None, drive.dostype)],
                       reserved_blocks=reserved)
    parts[0].bootable = drive.bootable
    parts[0].boot_priority = 0 if drive.bootable else -128
    #  Only the handler this drive actually needs; a card's RDB may carry
    #  several and the others mean nothing here.
    keep = [fs for fs in source.filesystems if fs.dostype == drive.dostype]
    return rdb.Rdb(geometry=geometry, partitions=parts, filesystems=keep,
                   cylinders=cylinders,
                   disk_vendor="PiStorm", disk_product=f"{drive.name} export")


def export(path: str | Path, chosen: Iterable[str], out_dir: str | Path,
           progress: Progress) -> list[Path]:
    """Write one self-contained ``.hdf`` per chosen drive. Returns the files."""
    path, out_dir = Path(path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    from . import builder                                    # noqa: PLC0415
    wanted = list(chosen)
    available = {d.name: d for d in drives(path)}
    missing = [n for n in wanted if n not in available]
    if missing:
        raise RuntimeError(
            f"{path.name} has no drive called {', '.join(missing)}; it holds "
            f"{', '.join(available) or 'none'}")
    if not wanted:
        raise RuntimeError("No drives were chosen to export.")

    written: list[Path] = []
    with open(path, "rb") as handle:
        located = builder.find_rdb(handle)
        if located is None:
            raise RuntimeError(f"{path.name} has no Rigid Disk Block")
        _base, source = located
        for name in wanted:
            drive = available[name]
            destination = out_dir / drive.filename()
            progress.step(f"Exporting {drive.name} to {destination.name}")
            progress.log(drive.description)
            table = _table_for(drive, source)
            if not table.filesystems:
                #  Said rather than hidden: the file will still be written,
                #  and for FFS an emulator supplies the handler itself.
                progress.log(f"  note: the card embeds no handler for "
                             f"{rdb.dostype_name(drive.dostype)}, so this file "
                             f"carries none either")
            with open(destination, "wb") as out:
                table.write(out, 0)
                out.seek(table.partitions[0].low_cyl
                         * table.geometry.cyl_blocks
                         * table.geometry.block_size)
                handle.seek(drive.start)
                _copy(handle, out, drive.size, progress)
            progress.log(f"  wrote {destination} ({human_size(drive.size)})")
            written.append(destination)
    return written


def _copy(source: BinaryIO, out: BinaryIO, size: int, progress: Progress) -> None:
    done = 0
    while done < size:
        block = source.read(min(CHUNK, size - done))
        if not block:
            #  A short read means the image ends before the RDB said it would.
            #  Pad rather than fail: a truncated card still has most of a
            #  drive on it, and saying so is more use than refusing.
            progress.log(f"  the image ends {human_size(size - done)} early; "
                         f"the rest of the file is zero")
            out.write(b"\0" * (size - done))
            return
        out.write(block)
        done += len(block)
        progress.fraction(done / size)
