"""Doing the network-facing preparation before a privileged write starts.

The GUI calls this as the ordinary user; the result is a directory of Emu68
files that :mod:`pistorm_imager.core.builder` can install without touching the
network, so the part of the run that needs root stays offline and local.
"""
from __future__ import annotations

import dataclasses
import re
import shutil
import tempfile
from pathlib import Path

from . import builder, kickstart, packages
from .util import Progress


def stage_emu68(config: builder.BuildConfig, progress: Progress) -> Path | None:
    """Download and unpack the chosen Emu68 build into a temporary directory."""
    if not config.install_emu68 or config.emu68_prepared_dir:
        return None
    staging = Path(tempfile.mkdtemp(prefix="pistorm-emu68-"))
    files, root = builder._prepare_emu68(  # noqa: SLF001 - one deliberate re-use
        builder.BuildConfig(
            variant=config.variant,
            release_tag=config.release_tag,
            emu68_archive=config.emu68_archive,
        ),
        staging,
        progress,
    )
    return root


#  Where the kernel says what is mounted, and how.
MOUNTINFO = Path("/proc/self/mountinfo")


def _unescape(field: str) -> str:
    """A mountinfo path, with its octal escapes - ``\\040`` is a space."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def user_only_mounts(mountinfo: Path = MOUNTINFO) -> list[Path]:
    """Mounts only this user can read: FUSE ones without ``allow_other``.

    A network share opened from the file manager is one (gvfs), as is an
    sshfs mount. The card is written by root under pkexec, and root is
    refused by such a mount however its permissions read - so a Kickstart
    folder on a NAS stopped the build with "Permission denied".
    """
    try:
        text = mountinfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        left, _, right = line.partition(" - ")
        fields, rest = left.split(), right.split()
        if len(fields) < 6 or len(rest) < 3 \
                or not (rest[0] == "fuse" or rest[0].startswith("fuse.")):
            continue
        options = set(fields[5].split(",")) | set(rest[2].split(","))
        if "allow_other" not in options:
            found.append(Path(_unescape(fields[4])))
    return found


def _under(path: str, mounts: list[Path]) -> bool:
    if not path:
        return False
    here = Path(path).absolute()
    return any(here == mount or mount in here.parents for mount in mounts)


def stage_user_only_inputs(config: builder.BuildConfig, into: Path,
                           progress: Progress,
                           mounts: list[Path] | None = None
                           ) -> builder.BuildConfig:
    """Copy what root cannot read to where it can, as the user, before pkexec.

    Every input on a user-only mount is copied into ``into`` and the
    configuration pointed at the copy. A folder of Kickstarts for WHDLoad
    is copied as the ROMs in it rather than whole: it is often a corner of a
    large collection. Everything else is copied as it is.
    """
    mounts = user_only_mounts() if mounts is None else mounts
    if not mounts:
        return config
    count = iter(range(1, 1_000_000))

    def copied(path: str) -> str:
        if not _under(path, mounts):
            return path
        source = Path(path)
        target = into / str(next(count)) / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        progress.log(f"Copying {source.name} from {source.parent}, which only "
                     f"you can read, so the card can be written from it")
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)
        return str(target)

    #  Said outright before the Kickstart is moved: WHDLoad's images default
    #  to the folder the card's Kickstart came from, and that would become
    #  a folder holding the one ROM.
    wants_roms = bool(packages.chosen_with(config.package_keys,
                                           "kickstart_drawer"))
    whdload = config.whdload_kickstarts
    if not whdload and config.kickstart_path:
        whdload = str(Path(config.kickstart_path).parent)
    if wants_roms and _under(whdload, mounts):
        roms = into / str(next(count)) / Path(whdload).name
        roms.mkdir(parents=True)
        progress.log(f"Copying the Kickstart ROMs in {whdload}, which only "
                     f"you can read")
        for info in kickstart.scan(whdload):
            shutil.copy2(info.path, roms / info.path.name)
        key = Path(whdload) / "rom.key"
        if key.is_file():
            shutil.copy2(key, roms / key.name)
        whdload = str(roms)
    elif whdload == str(Path(config.kickstart_path or ".").parent) \
            and not config.whdload_kickstarts:
        whdload = ""

    def spec(item: builder.AmigaPartitionSpec) -> builder.AmigaPartitionSpec:
        return dataclasses.replace(
            item, content_folder=copied(item.content_folder),
            content_hdf=copied(item.content_hdf),
            overlays=[(copied(src), dst) for src, dst in item.overlays])

    return dataclasses.replace(
        config,
        kickstart_path=copied(config.kickstart_path),
        kickstart_key=copied(config.kickstart_key),
        whdload_kickstarts=whdload,
        source_image=copied(config.source_image),
        hdf_image=copied(config.hdf_image),
        emu68_archive=copied(config.emu68_archive),
        pfs3_binary=copied(config.pfs3_binary),
        adf_folder=copied(config.adf_folder),
        spare_files_folder=copied(config.spare_files_folder),
        os_cd=copied(config.os_cd),
        boingbag_archives=[copied(p) for p in config.boingbag_archives],
        extra_boot_files=[copied(p) for p in config.extra_boot_files],
        amiga_partitions=[spec(p) for p in config.amiga_partitions],
        extra_partitions=[spec(p) for p in config.extra_partitions])
