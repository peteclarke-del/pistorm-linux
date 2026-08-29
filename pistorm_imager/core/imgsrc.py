"""Reading pre-built card images - PiMiga, Emu68 Hatcher, or any plain .img.

These arrive as multi-gigabyte downloads in whatever container the author chose,
so we stream them rather than unpacking to a temporary file: a 120 GB PiMiga
image would otherwise need 120 GB of scratch space before a single byte reached
the card.
"""
from __future__ import annotations

import bz2
import dataclasses
import gzip
import lzma
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path
from typing import BinaryIO

from .util import human_size

RAW_SUFFIXES = {".img", ".raw", ".bin", ".dd", ".iso", ".vhd"}
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}
STREAM_SUFFIXES = {".xz", ".gz", ".bz2", ".zst", ".lzma"}


@dataclasses.dataclass
class ImageSource:
    path: Path
    compression: str          # "none", "xz", "gz", "bz2", "zip", "7z", ...
    stored_size: int          # size of the file on disk
    expanded_size: int | None  # uncompressed size, when we can determine it
    member: str | None = None  # entry inside a zip/7z archive

    @property
    def description(self) -> str:
        if self.compression == "none":
            return f"{self.path.name} - {human_size(self.stored_size)}"
        size = human_size(self.expanded_size) if self.expanded_size else "unknown size"
        inner = f" [{self.member}]" if self.member else ""
        return (f"{self.path.name}{inner} - {self.compression}, "
                f"{human_size(self.stored_size)} compressed, {size} written")

    @property
    def write_size(self) -> int | None:
        return self.expanded_size if self.compression != "none" else self.stored_size


def _seven_zip() -> str | None:
    return shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")


def _archive_entries(path: Path) -> list[tuple[str, int]]:
    """List (name, uncompressed size) of disk-image-looking archive members."""
    suffix = path.suffix.lower()
    entries: list[tuple[str, int]] = []
    if suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            entries = [(i.filename, i.file_size) for i in zf.infolist() if not i.is_dir()]
    else:
        seven = _seven_zip()
        if seven is None:
            raise RuntimeError(
                f"{path.name} needs 7-Zip to open. Install the 'p7zip-full' package."
            )
        listing = subprocess.run([seven, "l", "-slt", str(path)],
                                 capture_output=True, text=True)
        if listing.returncode != 0:
            raise RuntimeError(f"could not list {path.name}: {listing.stderr.strip()}")
        name, size = None, 0
        for line in listing.stdout.splitlines():
            if line.startswith("Path = "):
                name = line[7:].strip()
            elif line.startswith("Size = "):
                try:
                    size = int(line[7:].strip() or 0)
                except ValueError:
                    size = 0
            elif not line.strip() and name:
                entries.append((name, size))
                name, size = None, 0
        if name:
            entries.append((name, size))
    #  Prefer things that look like disk images, biggest first.
    images = [e for e in entries if Path(e[0]).suffix.lower() in RAW_SUFFIXES]
    chosen = images or entries
    return sorted(chosen, key=lambda e: -e[1])


def _gzip_expanded_size(path: Path) -> int | None:
    """The ISIZE trailer, which is only the true size below 4 GiB."""
    with open(path, "rb") as handle:
        handle.seek(-4, 2)
        size = struct.unpack("<I", handle.read(4))[0]
    return size if size and path.stat().st_size < size else None


def _xz_expanded_size(path: Path) -> int | None:
    xz = shutil.which("xz")
    if xz is None:
        return None
    result = subprocess.run([xz, "--robot", "--list", str(path)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if fields[0] == "totals" and len(fields) > 5:
            try:
                return int(fields[5])
            except ValueError:
                return None
    return None


def inspect(path: str | Path) -> ImageSource:
    """Work out how to read ``path`` and how big it will be once written."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    stored = path.stat().st_size
    suffix = path.suffix.lower()

    if suffix in ARCHIVE_SUFFIXES:
        entries = _archive_entries(path)
        if not entries:
            raise RuntimeError(f"{path.name} contains no files")
        member, size = entries[0]
        return ImageSource(path, suffix.lstrip("."), stored, size or None, member)

    if suffix in STREAM_SUFFIXES:
        kind = suffix.lstrip(".")
        expanded = {"gz": _gzip_expanded_size, "xz": _xz_expanded_size}.get(
            kind, lambda _p: None)(path)
        return ImageSource(path, kind, stored, expanded)

    return ImageSource(path, "none", stored, stored)


def open_stream(source: ImageSource) -> tuple[BinaryIO, subprocess.Popen | None]:
    """Open a readable byte stream of the raw image.

    Returns the stream and, when an external decompressor is used, the process
    to reap afterwards.
    """
    path = str(source.path)
    if source.compression == "none":
        return open(path, "rb"), None
    if source.compression == "xz" or source.compression == "lzma":
        return lzma.open(path, "rb"), None
    if source.compression == "gz":
        return gzip.open(path, "rb"), None
    if source.compression == "bz2":
        return bz2.open(path, "rb"), None
    if source.compression == "zip":
        archive = zipfile.ZipFile(path)
        return archive.open(source.member), None
    if source.compression == "zst":
        zstd = shutil.which("zstd")
        if zstd is None:
            raise RuntimeError("zstd is required to read .zst images")
        proc = subprocess.Popen([zstd, "-dc", path], stdout=subprocess.PIPE)
        return proc.stdout, proc
    seven = _seven_zip()
    if seven is None:
        raise RuntimeError("7-Zip is required to read this archive")
    argv = [seven, "x", "-so", path]
    if source.member:
        argv.append(source.member)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc.stdout, proc
