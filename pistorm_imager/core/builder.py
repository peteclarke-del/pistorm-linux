"""The build orchestrator: turns a :class:`BuildConfig` into a written card.

Three ways to end up with a bootable PiStorm card are supported:

``FRESH``
    Partition the card ourselves - a FAT32 boot partition holding Emu68 plus an
    empty type 0x76 partition carrying an Amiga RDB, ready for HDToolBox or an
    AmigaOS install.

``IMAGE``
    Write a pre-built image (PiMiga, Emu68 Hatcher, a backup of your own card)
    and then re-apply your own Emu68 build, Kickstart and video settings on top.

``HDF``
    Take an Amiga hard disk image (an ``.hdf`` with a Rigid Disk Block, as used
    by WinUAE, FS-UAE and HstWB Installer), build the boot partition around it
    and write it into the 0x76 partition.  Unlike a PiMiga ``.img`` this is the
    Amiga drive alone, with no partition table or boot partition of its own.

``CUSTOMISE``
    Leave the card's data alone and only refresh the boot partition.

All three finish with the same customisation pass, so a PiMiga card gets exactly
the same config.txt handling as one we partitioned ourselves.
"""
from __future__ import annotations

import dataclasses
import datetime
import enum
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import (amigafs, amigaos, bootcfg, compat, content, devices, emu68,
               hdfcheck, imgsrc, kickstart, mbr, packages, pfs3, postwrite,
               rdb)
from .fat32 import Fat32
from .util import (MIB, Progress, align_up, copy_stream, human_size,
                   require_tool, run)

SECTOR = 512
#  Below this a 'use the remaining space' drive is not worth
#  formatting, and PFS3 has too few reserved blocks to work with.
MIN_DRIVE = 16 * MIB
DEFAULT_BOOT_START = 8192          # 4 MiB in - the usual SD alignment
DEFAULT_BOOT_SIZE = 256 * MIB
RDB_RESERVED_BLOCKS = 2016


class BuildMode(enum.Enum):
    FRESH = "fresh"
    IMAGE = "image"
    HDF = "hdf"
    CUSTOMISE = "customise"


@dataclasses.dataclass
class AmigaPartitionSpec:
    name: str = "DH0"                  # device name, as AmigaDOS mounts it
    size: int | None = None            # bytes; None means "the rest of the card"
    dostype: str = "PFS3"
    bootable: bool = False
    boot_priority: int = 0
    #  A host directory to copy into this partition once it is formatted.  This
    #  is how a directory-based emulator drive (PiMiga's disks/System and the
    #  like) becomes a real Amiga partition.
    content_folder: str = ""
    #  An Amiga hard disk image whose *contents* are copied into this partition.
    #  Unlike importing an .hdf wholesale, this reads the files out and writes
    #  them back, so the graphics driver and anything else can be adapted on
    #  the way in - and the partition can be a different size or file system.
    content_hdf: str = ""
    #  Which partition to take, when the source has more than one.
    content_hdf_partition: str = ""
    #  The volume label, which is what Workbench shows.  A drive copied from
    #  elsewhere keeps its own name rather than being relabelled after its
    #  device.  Declared last so that positional construction stays stable.
    volume_name: str = ""
    #  Extra files or folders laid on top afterwards, as (host path, destination
    #  inside the partition).  Used to add WHDLoad to a Workbench installed
    #  from floppies, which has no idea such a thing exists.
    overlays: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    #  Paths within content_folder to leave out, matched case-insensitively
    #  against the start of each entry's relative path.  Chipset-specific game
    #  collections are skipped this way on machines that cannot run them.
    exclude: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class BuildConfig:
    mode: BuildMode = BuildMode.FRESH
    target: str = ""                   # /dev/sdX or a path to an .img file
    target_is_device: bool = False
    image_size: int = 8 * 1024 * MIB   # only used when creating a new .img file

    #  Emu68
    variant: str = "pistorm32lite"
    release_tag: str = ""              # empty means "the newest stable release"
    emu68_archive: str = ""            # a local zip instead of downloading
    emu68_prepared_dir: str = ""       # already-unpacked files (see below)
    install_emu68: bool = True

    #  Source image (IMAGE mode) / Amiga hard disk image (HDF mode)
    source_image: str = ""
    hdf_image: str = ""

    #  Partitioning (FRESH mode)
    boot_size: int = DEFAULT_BOOT_SIZE
    amiga_partitions: list[AmigaPartitionSpec] = dataclasses.field(
        default_factory=lambda: [AmigaPartitionSpec("DH0", None, "PFS3", True, 0)])
    pfs3_binary: str = ""              # optional pfs3aio to embed in the RDB
    #  Optional software, fetched from its publisher while the card is built
    #  and cached between builds.
    package_keys: list[str] = dataclasses.field(default_factory=list)
    #  What to do when a drive being imported already has a program that was
    #  ticked here. The file system creates files and never overwrites them,
    #  so whichever lands first wins - and that used to be settled by the
    #  order the build happened to run in rather than by anybody's choice.
    replace_older_software: bool = True
    package_chipset: str = ""          # a machines.Chipset value
    package_display: str = ""          # a machines.Display value

    #  Installing AmigaOS from Workbench floppy images
    install_amigaos: bool = False
    #  Apply PiStorm compatibility fixes to copied systems automatically.
    fix_compatibility: bool = True
    #  Where the operating system came from ("adf" or "pimiga"); informational.
    system_source: str = "adf"
    #  Whether the machine is being watched on an RTG display, which decides
    #  whether an emulator's graphics driver is replaced or simply removed.
    rtg_display: bool = False
    #  Whether the Amiga's own video output is also in use.  Both can be true:
    #  RTG on the Pi's HDMI with a monitor still on the Amiga's video port.
    native_display: bool = False
    #  Which of the two Workbench should open on when there is a choice.
    workbench_on_rtg: bool = True
    #  A folder of known-good replacement files, used if a source file cannot
    #  be read (a transient error on a loop-mounted image, say).
    spare_files_folder: str = ""
    adf_folder: str = ""
    adf_version: str = ""              # "" means "work it out from the disks"
    amiga_volume_name: str = "Workbench"

    #  Boot configuration
    boot_options: bootcfg.BootOptions = dataclasses.field(
        default_factory=bootcfg.BootOptions)
    kickstart_path: str = ""
    kickstart_key: str = ""
    kickstart_name: str = "kick.rom"
    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_country: str = "GB"

    #  Output shape: a whole SD card, or just an Amiga hard disk image
    output_hdf: bool = False
    #  Repair the RDB of an imported image for PiStorm compatibility
    repair_rdb: bool = True
    #  After writing a prepared system, clear a saved screen mode that would
    #  put Workbench on a screen this machine has not got.  Off by default:
    #  it edits a system the user chose deliberately.
    patch_display: bool = False

    #  Post-processing
    expand_to_fill: bool = False
    #  Partitions to create in whatever space an imported drive leaves unused.
    #  Sizes are honoured in order; the last one with size None takes the rest.
    extra_partitions: list[AmigaPartitionSpec] = dataclasses.field(
        default_factory=lambda: [AmigaPartitionSpec("DH1", None, "PFS3", False, -128)])
    extra_boot_files: list[str] = dataclasses.field(default_factory=list)

    def concerns(self) -> list[str]:
        """Choices that will build, and probably are not what was meant.

        Distinct from validate(), which refuses. These are combinations that
        produce a working card doing something other than what the settings
        suggest - a games drive with nothing able to launch a game, a card
        told to use an RTG screen with no RTG driver on it. They are said
        before anything is written, and the build goes ahead anyway.
        """
        said: list[str] = []
        keys = set(self.package_keys or ())
        filled = [p for p in self.amiga_partitions
                  if p.content_folder or p.content_hdf]
        names = " ".join((p.volume_name or p.name) + " " + (p.content_folder or "")
                         for p in filled).lower()

        if ("whdload" not in keys
                and any(word in names for word in ("game", "demo", "whdload"))):
            said.append(
                "There are games or demos on this card and WHDLoad is not "
                "installed, so nothing on it can launch them.")
        if "igame" in keys and not any(
                "game" in (p.volume_name or p.name).lower() for p in filled):
            said.append(
                "iGame is installed and no drive is being filled with games, "
                "so it will open on an empty list.")
        if self.rtg_display and "picasso96" not in keys \
                and not any(p.content_hdf or p.content_folder
                            for p in self.amiga_partitions if p.bootable):
            said.append(
                "This card is set up for an RTG screen on the Pi's HDMI "
                "output, but Picasso96 is not being installed and no system "
                "is being imported that might carry it, so there will be no "
                "RTG screen to open on.")
        if self.workbench_on_rtg and not self.rtg_display:
            said.append(
                "Workbench is set to open on the RTG screen, and this card "
                "has no RTG display configured.")
        if not self.install_amigaos and not filled and self.mode is BuildMode.FRESH:
            said.append(
                "Nothing is being put on the Amiga drives: no Workbench, no "
                "imported drive and no folder, so the card will boot to a "
                "screen asking for a disk.")
        by_hand = sorted(key for key in keys
                         if (packages.CATALOGUE_BY_KEY.get(key) is not None
                             and packages.CATALOGUE_BY_KEY[key].download
                             is not None
                             and packages.CATALOGUE_BY_KEY[key]
                             .download.manual))
        if by_hand:
            said.append(
                f"{', '.join(by_hand)} cannot be downloaded here - its "
                f"publisher serves it only to a browser - so put the archive "
                f"in the cache first, or it will be left out.")
        return said

    def brings_a_system_from_elsewhere(self) -> bool:
        """Whether a system somebody else set up is going onto this card.

        A whole prepared image and a drive written unchanged both are one; so
        is a drive imported onto a card this build partitions, which is what
        was missed - its saved screen mode is just as much somebody else's as
        the other two, and nothing was ever done about it.
        """
        if self.mode is not BuildMode.FRESH:
            return True
        return any(p.content_hdf for p in self.amiga_partitions)

    def validate(self) -> list[str]:
        """Return a list of problems; an empty list means the config is usable."""
        problems: list[str] = []
        if not self.target:
            problems.append("No target selected.")
        if self.mode is BuildMode.IMAGE and not self.source_image:
            problems.append("No source image selected.")
        if self.mode is BuildMode.IMAGE and self.source_image \
                and not Path(self.source_image).is_file():
            problems.append(f"Source image not found: {self.source_image}")
        if self.mode is BuildMode.HDF:
            if not self.hdf_image:
                problems.append("No Amiga hard disk image (.hdf) selected.")
            elif not Path(self.hdf_image).is_file():
                problems.append(f"Hard disk image not found: {self.hdf_image}")
            if self.boot_size < 64 * MIB:
                problems.append("The boot partition must be at least 64 MiB.")
        if self.kickstart_path and not Path(self.kickstart_path).is_file():
            problems.append(f"Kickstart ROM not found: {self.kickstart_path}")
        if self.emu68_archive and not Path(self.emu68_archive).is_file():
            problems.append(f"Emu68 archive not found: {self.emu68_archive}")
        if self.mode is BuildMode.FRESH:
            if self.boot_size < 64 * MIB and not self.output_hdf:
                problems.append("The boot partition must be at least 64 MiB.")
            if not self.amiga_partitions:
                problems.append("Define at least one Amiga partition.")
            flexible = [p for p in self.amiga_partitions if p.size is None]
            if len(flexible) > 1:
                problems.append(
                    "Only one Amiga partition can be set to 'use remaining space'.")
            #  Everything has to fit the card that was asked for. Nothing
            #  checked, so a layout larger than the image was accepted and
            #  the drives were simply laid out past the end of it.
            overhead = 0 if self.output_hdf else (
                (DEFAULT_BOOT_START * SECTOR) + self.boot_size)
            fixed = sum(p.size or 0 for p in self.amiga_partitions)
            flexible = any(p.size is None for p in self.amiga_partitions)
            if overhead + fixed > self.image_size:
                over = overhead + fixed - self.image_size
                with_boot = ("" if self.output_hdf else
                             f", which with the {human_size(self.boot_size)} "
                             f"boot partition")
                problems.append(
                    f"The drives add up to {human_size(fixed)}{with_boot} is "
                    f"{human_size(over)} more than the "
                    f"{human_size(self.image_size)} asked for. Note that "
                    f"'125G' means 125 GiB; a card sold as 125 GB is "
                    f"'125GB'.")
            elif flexible and (self.image_size - overhead - fixed) < MIN_DRIVE:
                left = self.image_size - overhead - fixed
                problems.append(
                    f"The drives with a fixed size leave only "
                    f"{human_size(max(left, 0))} of the "
                    f"{human_size(self.image_size)} for the one set to use "
                    f"the remaining space, which is too small to be a drive.")
            #  A drive filled from elsewhere may still need the Workbench
            #  disks: ClassicWB's boots and asks for them, because Commodore's
            #  files cannot be given away. The content is written first and
            #  the disks fill in what it does not have, so both are allowed -
            #  but a folder to find the disks in is not optional then.
            if self.install_amigaos and not self.adf_folder:
                problems.append("No folder of Workbench disk images was given.")
        if self.output_hdf:
            if self.mode is not BuildMode.FRESH:
                problems.append(
                    "An Amiga hard disk image can only be produced when building "
                    "a new drive.")
            if self.target_is_device:
                problems.append(
                    "An Amiga hard disk image must be written to a file, not to a "
                    "card.")
        for spec in self.amiga_partitions:
            if spec.content_hdf and not Path(spec.content_hdf).is_file():
                problems.append(
                    f"{spec.name}: image not found: {spec.content_hdf}")
            if spec.content_folder and not Path(spec.content_folder).is_dir():
                problems.append(
                    f"{spec.name}: folder not found: {spec.content_folder}")
            elif (spec.content_folder or spec.content_hdf) \
                    and not spec.dostype.upper().startswith(
                        ("FFS", "PFS", "PDS")):
                problems.append(
                    f"{spec.name} is set to {spec.dostype}; content can only be "
                    f"written to an FFS or PFS3 partition.")
        if self.install_amigaos:
            if self.mode is not BuildMode.FRESH:
                problems.append(
                    "AmigaOS can only be installed when building a new card.")
            if not self.adf_folder:
                problems.append("No folder of Workbench ADF disks selected.")
            elif not Path(self.adf_folder).is_dir():
                problems.append(f"ADF folder not found: {self.adf_folder}")
            target = next((p for p in self.amiga_partitions if p.bootable),
                          self.amiga_partitions[0] if self.amiga_partitions else None)
            if target is not None and not target.dostype.upper().startswith(
                    ("FFS", "PFS", "PDS")):
                problems.append(
                    f"AmigaOS is installed onto {target.name}, which must use "
                    f"FFS or PFS3 (it is set to {target.dostype})."
                )
        if self.wifi_ssid and not self.wifi_password:
            problems.append("A WiFi network was given without a password.")
        return problems


# ---------------------------------------------------------------- helpers


def _target_size(config: BuildConfig) -> int:
    if config.target_is_device:
        return devices.device_size(config.target)
    path = Path(config.target)
    if path.exists() and config.mode is not BuildMode.FRESH:
        return path.stat().st_size
    return config.image_size


def _open_target(config: BuildConfig, create_size: int | None = None):
    """Open the destination for read/write, creating an image file if needed."""
    path = Path(config.target)
    if not config.target_is_device:
        if create_size is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as handle:
                handle.truncate(create_size)
        elif not path.exists():
            raise FileNotFoundError(f"{path} does not exist")
    #  Buffered on purpose: a raw (buffering=0) file object may legally write
    #  fewer bytes than asked for, which would silently truncate a card write.
    return open(path, "r+b")


def _find_boot_partition(parts: list[mbr.MbrPartition]) -> mbr.MbrPartition:
    for part in parts:
        if part.type_id in (mbr.TYPE_FAT32_CHS, mbr.TYPE_FAT32_LBA) and not part.empty:
            return part
    #  Some images use a plain FAT16 id for a small boot partition.
    for part in parts:
        if part.type_id in (0x01, 0x04, 0x06, 0x0E) and not part.empty:
            return part
    raise RuntimeError("no FAT boot partition found in the partition table")


def _find_amiga_partition(parts: list[mbr.MbrPartition]) -> mbr.MbrPartition | None:
    for part in parts:
        if part.type_id == mbr.TYPE_AMIGA and not part.empty:
            return part
    return None


def describe_target(config: BuildConfig) -> str:
    """A human summary of what the target looks like right now."""
    try:
        size = _target_size(config)
    except OSError as error:
        return f"Cannot read target: {error}"
    lines = [f"Target: {config.target} ({human_size(size)})"]
    try:
        with open(config.target, "rb") as handle:
            parts = mbr.read_table(handle)
            lines.append("Existing partitions:")
            lines.append(mbr.describe(parts))
            amiga = _find_amiga_partition(parts)
            if amiga:
                try:
                    table = rdb.Rdb.read(handle, amiga.start_bytes)
                    lines.append("Amiga RDB:")
                    lines.append(table.describe())
                except ValueError as error:
                    lines.append(f"Amiga partition present but no usable RDB ({error})")
    except (OSError, ValueError) as error:
        lines.append(f"(no partition table: {error})")
        try:
            with open(config.target, "rb") as handle:
                table = rdb.Rdb.read(handle, 0)
            lines.append("This looks like an Amiga hard disk image (RDB at block 0):")
            lines.append(table.describe())
        except (OSError, ValueError):
            pass
    return "\n".join(lines)


# ------------------------------------------------------------ build stages


def _prepare_emu68(config: BuildConfig, workdir: Path,
                   progress: Progress) -> tuple[list[Path], Path]:
    """Get the Emu68 files, adding Raspberry Pi firmware if the release omits it.

    ``emu68_prepared_dir`` short-circuits the download.  The GUI uses it so that
    all network access happens as the ordinary user before the privileged writer
    starts: the half that runs as root then only touches the card.
    """
    if config.emu68_prepared_dir:
        root = Path(config.emu68_prepared_dir)
        files = sorted(p for p in root.rglob("*") if p.is_file())
        if not files:
            raise RuntimeError(f"no Emu68 files found in {root}")
        progress.log(f"Using {len(files)} prepared Emu68 files from {root}")
        return files, root

    unpacked = workdir / "emu68"
    if config.emu68_archive:
        archive = Path(config.emu68_archive)
        progress.log(f"Using local Emu68 archive {archive.name}")
    else:
        progress.step("Fetching the Emu68 release")
        releases = emu68.fetch_releases()
        if config.release_tag:
            match = next((r for r in releases if r.tag == config.release_tag), None)
            if match is None:
                raise RuntimeError(f"release {config.release_tag} not found")
        else:
            match = next((r for r in releases
                          if not r.prerelease and emu68.has_variant(r, config.variant)), None)
            if match is None:
                raise RuntimeError("no stable Emu68 release found for this board")
        progress.log(f"Emu68 {match.display()} for "
                     f"{emu68.VARIANTS_BY_KEY[config.variant].label}")
        archive = emu68.get_release_archive(match, config.variant, progress)

    progress.step("Unpacking Emu68")
    files = emu68.extract(archive, unpacked, progress)
    if emu68.needs_firmware(files):
        progress.step("Downloading Raspberry Pi boot firmware")
        progress.log("This Emu68 release does not bundle the Raspberry Pi firmware.")
        files += emu68.fetch_firmware(unpacked, progress)
    return files, unpacked


def _make_boot_filesystem(size: int, workdir: Path, progress: Progress) -> Path:
    """Create and format a FAT32 image of ``size`` bytes as a plain file."""
    require_tool("mkfs.vfat", "dosfstools")
    boot = workdir / "boot.img"
    with open(boot, "wb") as handle:
        handle.truncate(size)
    run(["mkfs.vfat", "-F", "32", "-n", "EMU68BOOT", str(boot)], log=progress.log)
    return boot


def _populate_boot(fs: Fat32, config: BuildConfig, emu68_files: list[Path],
                   emu68_root: Path | None, progress: Progress) -> None:
    """Write Emu68, the Kickstart and the configuration onto the boot partition."""
    options = dataclasses.replace(config.boot_options)

    if emu68_files:
        progress.step("Copying Emu68 to the boot partition")
        template: bootcfg.ConfigTxt | None = None
        payload = [p for p in emu68_files if p.name != "config.txt"]
        for index, path in enumerate(emu68_files, start=1):
            progress.check_cancelled()
            relative = path.relative_to(emu68_root) if emu68_root else Path(path.name)
            if path.name == "config.txt":
                template = bootcfg.ConfigTxt.load(path)
                continue
            fs.write_file(str(relative).replace(os.sep, "/"), path)
            progress.log(f"  {relative}")
            progress.fraction(index / max(1, len(emu68_files)))
        kernel = emu68.kernel_name(payload)
        if kernel and not options.kernel:
            options.kernel = kernel
    else:
        template = None

    if template is None:
        #  Updating an existing card without reinstalling Emu68: edit in place.
        try:
            template = bootcfg.ConfigTxt(fs.read_bytes("config.txt").decode(
                "utf-8", errors="replace"))
            progress.log("Editing the config.txt already on the card")
        except Exception:  # noqa: BLE001 - a missing/unreadable file is fine
            template = bootcfg.ConfigTxt("# Written by PiStorm Imager\n")

    if config.kickstart_path:
        progress.step("Installing the Kickstart ROM")
        info = kickstart.identify(config.kickstart_path, config.kickstart_key or None)
        progress.log(f"{info.name}" + (f" ({info.note})" if info.note else ""))
        if not info.aga:
            progress.log("WARNING: this is not an AGA (A1200) Kickstart. "
                         "Emu68 expects an A1200 ROM.")
        data = kickstart.prepare(info, config.kickstart_key or None)
        fs.write_bytes(config.kickstart_name, data)
        options.kickstart_file = config.kickstart_name

    progress.step("Writing the boot configuration")
    options.apply_config(template)
    fs.write_bytes("config.txt", template.to_bytes())
    progress.log("config.txt written")

    cmdline = options.cmdline()
    if cmdline:
        fs.write_bytes("cmdline.txt", (cmdline + "\n").encode("utf-8"))
        progress.log(f"cmdline.txt: {cmdline}")
    elif fs.exists("cmdline.txt"):
        fs.remove("cmdline.txt")

    if config.wifi_ssid:
        conf = bootcfg.wifi_config(config.wifi_ssid, config.wifi_password,
                                   config.wifi_country)
        fs.write_bytes("wpa_supplicant.conf", conf.encode("utf-8"))
        progress.log(f"WiFi configured for network {config.wifi_ssid!r}")

    for extra in config.extra_boot_files:
        path = Path(extra)
        if path.is_file():
            fs.write_file(path.name, path)
            progress.log(f"Extra file: {path.name}")

    fs.flush()


def _build_rdb(config: BuildConfig, partition_blocks: int,
               progress: Progress) -> rdb.Rdb:
    """Lay out the Amiga partitions inside the 0x76 partition."""
    geometry = rdb.Geometry()
    specs = []
    for spec in config.amiga_partitions:
        specs.append((spec.name, spec.size, rdb.parse_dostype(spec.dostype)))
    parts = rdb.layout(geometry, partition_blocks, specs,
                       reserved_blocks=RDB_RESERVED_BLOCKS)
    for part, spec in zip(parts, config.amiga_partitions):
        part.bootable = spec.bootable
        part.boot_priority = spec.boot_priority

    filesystems = _filesystem_drivers(config, parts, progress)

    table = rdb.Rdb(
        geometry=geometry,
        partitions=parts,
        filesystems=filesystems,
        cylinders=geometry.cylinders_for(partition_blocks),
        rdb_blocks_hi=RDB_RESERVED_BLOCKS - 1,
    )
    return table


VERSION_STRING = re.compile(rb"\$VER:[^\x00]{0,60}?(\d+)\.(\d+)")


def handler_version(binary: bytes) -> int:
    """Read a file system handler's version from its embedded $VER: string.

    The RDB records a version so AmigaOS can tell one handler from another;
    embedding a handler as version 0.0 would let any other copy on the system
    take precedence over it.
    """
    match = VERSION_STRING.search(binary)
    if match is None:
        return 0
    major, minor = int(match.group(1)), int(match.group(2))
    return (major << 16) | minor


def _filesystem_drivers(config: BuildConfig, parts: list[rdb.Partition],
                        progress: Progress) -> list[rdb.FileSystem]:
    """Work out which file system handlers to embed in the RDB we are writing.

    ``pfs3_binary`` may be a plain handler file, or another RDB image (an .hdf,
    or a card) to lift the handlers out of - which is convenient, because a
    ready-made image such as an HstWB install already carries a matching PFS3.
    """
    if not config.pfs3_binary:
        return []
    path = Path(config.pfs3_binary)
    needed = {p.dostype for p in parts}
    #  The donor may be a bare .hdf or a whole card image such as a PiMiga
    #  download; either way we only want the handlers out of its RDB.
    with open(path, "rb") as handle:
        located = find_rdb(handle)
        donor = located[1] if located else None
    if donor is not None and donor.filesystems:
        chosen = [fs for fs in donor.filesystems if fs.dostype in needed] \
            or donor.filesystems
        for fs in chosen:
            progress.log(f"Taking the {rdb.dostype_name(fs.dostype)} handler "
                         f"({len(fs.seglist)} bytes) from {path.name}")
        return chosen
    if donor is not None:
        progress.log(f"{path.name} has an RDB but no file system handlers in it")
        return []

    binary = path.read_bytes()
    version = handler_version(binary)
    wanted = [dt for dt in (rdb.DOSTYPE_PFS3, rdb.DOSTYPE_PDS3) if dt in needed]
    if not wanted:
        wanted = [rdb.DOSTYPE_PFS3]
    out = []
    for dostype in wanted:
        out.append(rdb.FileSystem(dostype=dostype, seglist=binary,
                                  version=version))
        shown = f" version {version >> 16}.{version & 0xFFFF}" if version else ""
        progress.log(f"Embedding {path.name} ({len(binary)} bytes) as "
                     f"{rdb.dostype_name(dostype)}{shown}")
    return out


def _add_missing_system(config: BuildConfig, volume, fixer,
                        progress: Progress) -> None:
    """Put what the Workbench disks have, and an imported drive has not, on it.

    A drive can boot and still bring no operating system: ClassicWB's carries
    no C:LoadWB, no C:IPrefs and no workbench.library, because those are
    Commodore's and cannot be given away, and its first boot asks for a
    Workbench disk to copy them from. Nothing already on the drive is
    replaced - what its author put there is what they meant.
    """
    disks = amigaos.scan(config.adf_folder, progress)
    chosen = amigaos.choose_set(disks, config.adf_version)
    if not chosen:
        progress.log(f"WARNING: no Workbench disks in {config.adf_folder}, so "
                     f"the drive keeps whatever it came with")
        return
    progress.step("Adding what the Workbench disks have and the drive has not")
    added = 0
    for match in sorted(chosen.values(), key=lambda m: m.role.order):
        with open(match.path, "rb") as handle:
            source = amigaos.Volume(handle)
            copied, _skipped = amigaos.copy_volume(
                source, volume, match.role.destination, progress,
                skip_existing=True, compat=fixer)
        added += copied
        progress.log(f"  {match.role.label}: {copied} file(s) the drive "
                     f"did not have")
    progress.log(f"{added} file(s) added from the Workbench disks")


def _install_amigaos(config: BuildConfig, handle, amiga: mbr.MbrPartition,
                     table: rdb.Rdb, progress: Progress) -> None:
    """Format the boot partition and copy Workbench onto it from ADFs."""
    partition = next((p for p in table.partitions if p.bootable),
                     table.partitions[0] if table.partitions else None)
    if partition is None:
        raise RuntimeError("there is no Amiga partition to install onto")

    progress.step("Looking for Workbench disks")
    disks = amigaos.scan(config.adf_folder, progress)
    progress.log(f"Found {len(disks)} recognisable install disks")
    versions = amigaos.available_versions(disks)
    if versions:
        progress.log("AmigaOS releases available: " + ", ".join(versions))
    chosen = amigaos.choose_set(disks, config.adf_version)
    if not chosen:
        raise RuntimeError(
            f"No Workbench install disks were found in {config.adf_folder}")
    for match in sorted(chosen.values(), key=lambda m: m.role.order):
        progress.log(f"  {match.role.label:10} {match.label}")
    missing = amigaos.missing_roles(chosen)
    if missing:
        raise RuntimeError("Missing the " + ", ".join(r.label for r in missing)
                           + " disk, which is required for an install.")

    needed = amigaos.estimate_size(chosen)
    available = partition.size_bytes(table.geometry)
    if available < needed:
        raise RuntimeError(
            f"{partition.drive_name} is {human_size(available)}, but the install "
            f"needs about {human_size(needed)}."
        )
    #  The limit is the ROM file system's, so it is not a fact about size: a
    #  PFS3 volume is meant to be bigger than this, and warning about it sends
    #  people off shrinking a partition that was right all along.
    if (available > amigafs.FFS_SAFE_LIMIT
            and amigafs.is_dos_family(partition.dostype)
            and amigafs.is_ffs(partition.dostype)):
        progress.log(
            f"WARNING: {partition.drive_name} is {human_size(available)} and "
            f"formatted FFS. AmigaOS 3.1's FFS is unreliable above "
            f"{human_size(amigafs.FFS_SAFE_LIMIT)}; a smaller system "
            f"partition, or PFS3, is safer."
        )

    offset = partition.byte_offset(table.geometry, amiga.start_bytes)
    dostype = partition.dostype
    if not amigafs.is_ffs(dostype) and dostype not in (rdb.DOSTYPE_PFS3,
                                                       rdb.DOSTYPE_PDS3):
        raise RuntimeError(
            f"{partition.drive_name} is {rdb.dostype_name(dostype)}; AmigaOS can "
            f"only be installed onto an FFS or PFS3 partition."
        )
    editor = _startup_sequence_editor(config, progress)
    #  One pass for the whole partition. Each overlay used to get its own, so
    #  no single one of them ever saw everything the card was given - and the
    #  floppies were seen by none of them.
    fixer = _make_fixer(config, progress)
    #  Resolved before the disks are copied, not after: the file system here
    #  creates files and never overwrites them, so whatever lands first wins.
    #  Asked for afterwards, a package's current release lost to whatever
    #  Workbench 3.1 shipped in 1994 - PeterK's icon.library among them.
    spec = next((s for s in config.amiga_partitions
                 if s.name.upper() == partition.drive_name.upper()), None)
    credit: dict[tuple[str, str], str] = {}
    landings: dict = {}
    extra = _package_overlays(config, list(spec.overlays), progress, credit) \
        if spec is not None else []
    if extra and config.replace_older_software:
        fixer.displace(_landing_paths(extra))
    #  Any record an imported drive brings describes a card that no longer
    #  exists; this build writes its own in its place.
    fixer.displace([MANIFEST_PATH])
    if _package_startup_lines(config):
        fixer.keep_user_startup()
    volume = amigaos.install(handle, offset, partition.blocks(table.geometry),
                             chosen, progress,
                             volume_name=config.amiga_volume_name,
                             dostype=dostype, close=False,
                             edit=_BothPasses(editor, fixer))
    if editor is not None and not editor.inserted:
        progress.log("WARNING: S:Startup-Sequence could not be edited, so the "
                     "icon.library on disk will not replace the one in ROM "
                     "and OS3.5 colour icons will not be drawn")
    #  Overlays (WHDLoad and the like) go on while the volume is still open;
    #  reopening a finished volume would mean rebuilding its allocation state.
    fixer.stop_displacing()
    if spec is not None:
        if spec.overlays or extra:
            spec = dataclasses.replace(spec,
                                       overlays=list(spec.overlays) + extra)
            _apply_overlays(volume, spec, fixer, progress, landings)
        _give_drawers_icons(volume, spec, config, progress)
    _write_user_startup(volume, config, progress, fixer.kept_user_startup)
    _write_manifest(volume, config,
                    list(spec.overlays) if spec is not None else [],
                    credit, progress, landings)
    #  Now that everything is on it, and not before: the graphics driver and
    #  the display-switching scripts depend on what the packages installed.
    fixer.finish(volume, progress)
    progress.step("Finalising the Amiga file system")
    volume.close()


class _BothPasses:
    """Show every file to the startup editor and to the compatibility pass.

    The floppies were only ever shown to the editor, so no compatibility rule
    saw the operating system itself - only the packages laid on top of it. A
    monitor driver sitting in the Storage disk was invisible, and a card built
    from floppies went out with no way to choose a screen mode.
    """

    #  The editor finishes with each tree, because it has to put its lines
    #  into the Startup-Sequence as the disk carrying it is copied. The
    #  compatibility pass is finished once, by the builder, when the volume
    #  is full.
    finish_with_each_tree = True

    def __init__(self, editor, fixer):
        self.editor = editor
        self.fixer = fixer

    def offer(self, relative: str, data: bytes) -> bytes:
        if self.editor is not None:
            data = self.editor.offer(relative, data)
        return self.fixer.offer(relative, data)

    def skip(self, relative: str) -> bool:
        if self.editor is not None and self.editor.skip(relative):
            return True
        return self.fixer.skip(relative)

    def finish(self, target, progress: Progress) -> None:
        if self.editor is not None:
            self.editor.finish(target, progress)


def _startup_sequence_editor(config: BuildConfig, progress: Progress):
    """What has to run before Workbench draws its first icon.

    Only ``icon.library`` so far, and only when it was chosen.  It has to be
    soft-kicked before ``IPrefs`` opens the ROM one, which is why this cannot
    live in ``S:User-Startup`` with the rest of the package startup lines.
    """
    chosen = packages.expand(config.package_keys)
    if "iconlib" not in chosen:
        return None
    #  LoadModule, not LoadResident.  LoadResident cannot displace a library
    #  that is already in the system list, and icon.library is there from the
    #  moment the machine starts; LoadModule loads the replacement and soft
    #  resets so it is in place from the next boot onwards.  This is exactly
    #  what the ready-made distributions do, early in their own startup.
    #  AUTO, and a guard on LoadModule itself.
    #
    #  LoadModule installs the modules and soft resets so they are in place
    #  from the next boot.  Without AUTO it resets every time, and on a card
    #  where the modules do not survive the reset that is a loop: the machine
    #  resets, runs this again, resets again, and never reaches Workbench.
    #  A card was left doing exactly that, two resets deep, with a black
    #  screen.  AUTO resets only when it has actually installed something,
    #  so the second pass finds them resident and carries on.
    #
    #  The IF EXISTS on C:LoadModule matters too: without the soft-kick the
    #  icons still draw badly, but a Startup-Sequence that calls a command
    #  that is not there is worse than one that skips it.
    return amigaos.StartupSequenceEditor(
        ["IF EXISTS C:LoadModule",
         "   IF EXISTS LIBS:icon.library",
         "      C:LoadModule AUTO LIBS:workbench.library LIBS:icon.library",
         "   EndIF",
         "EndIF"], progress)


def _package_startup_lines(config: "BuildConfig") -> list[str]:
    """The lines the chosen software needs in S:User-Startup."""
    lines: list[str] = []
    #  expand() so that a package pulled in as a dependency gets its lines
    #  too: MUI is never ticked by name, and without its assigns muimaster
    #  cannot be found however completely its files were copied.
    for key in packages.expand(config.package_keys):
        package = packages.CATALOGUE_BY_KEY.get(key)
        if package is not None and package.startup:
            lines += list(package.startup)
    return lines


def _write_user_startup(volume, config: "BuildConfig",
                        progress: Progress, kept: bytes = b"") -> None:
    """Add the lines the chosen packages need to S:User-Startup.

    Workbench 3.1 runs this from its own Startup-Sequence if it is there, so
    it is where a package that has to be started, or soft-kicked over a ROM
    module, gets its chance.  Copying the file into LIBS: alone would leave
    the ROM version in use and the whole package inert.
    """
    lines = _package_startup_lines(config)
    if not lines:
        return
    folder = volume.makedirs("S")
    #  _entry_exists is the lookup both writers share.  find_entry is the PFS3
    #  writer's own, and reaching for it worked on a PFS3 system drive and
    #  crashed the build at the last step on an FFS one.
    if volume._entry_exists(folder, "User-Startup") is not None:
        progress.log("  S:User-Startup already exists; left alone")
        return
    #  A drive being imported brings its own, which was held back during the
    #  copy so it could be written here with the packages' lines after it.
    #  Left whole and appended to: it is the distribution's own setup and
    #  replacing it would break the system this card is built on.
    head = ""
    if kept:
        head = kept.decode("latin-1")
        if not head.endswith("\n"):
            head += "\n"
    body = (head
            + "\n; Added by the PiStorm imager for the software you chose.\n"
            + "\n".join(lines) + "\n")
    volume.write_file(folder, "User-Startup", body.encode("latin-1"),
                      check_existing=False)
    progress.log(f"  S:User-Startup written ({len(lines)} lines)")


#  Where the record of this build lands on the card.  S: because that is the
#  drawer a system's own bookkeeping lives in, and it is reachable as
#  SYS:S/PiStorm-Installed from a shell on any drive.
MANIFEST_PATH = "S/PiStorm-Installed"

#  A package merged into a drawer the system already owns is listed file by
#  file.  Past this many, the drawer is named once with a count instead - a
#  removal list nobody can read is no better than none.
MANIFEST_FILE_LIMIT = 200

#  Drawers that belong to AmigaOS, to Workbench, or to the card itself, and
#  are therefore never named in the record as something to delete.  Getting
#  this wrong in the other direction is the only way this file can do harm:
#  "Delete SYS:C ALL" takes AmigaDOS with it.
#
#  The bare drawers are the dangerous ones, and every package that merges into
#  one does so under exactly its own name - WHDLoad's commands into "C",
#  MUI's classes into "Libs" - so a rule based on the source drawer's name
#  matching would name C, Libs and S as whole drawers.  It is the destination
#  that decides, not the name it arrived under.
SYSTEM_DRAWERS = {
    "", "c", "s", "l", "libs", "devs", "prefs", "fonts", "locale",
    "utilities", "tools", "system", "wbstartup", "storage", "classes",
    "expansion", "rexx", "trashcan", "monitors", "disk",
    #  Ones this build creates to hold other things, which several packages
    #  land in side by side.
    "programs", "internet", "audio", "games", "demos", "storage/install",
    #  Nested drawers AmigaOS owns.
    "locale/catalogs", "locale/help", "prefs/env-archive", "prefs/presets",
    "devs/networks", "devs/dosdrivers", "devs/monitors", "devs/keymaps",
    "devs/printers", "libs/mui", "system/mui/libs/mui",
}


def _manifest_entries(pairs: list[tuple[str, str]],
                      credit: dict[tuple[str, str], str],
                      landings: dict | None = None
                      ) -> list[tuple[str, list[str]]]:
    """What each package really put on the card, as paths that can be deleted.

    Built from what the copy actually wrote, never from what was asked for.
    The first version walked the source tree instead and claimed five of
    ClassicWB's own libraries as NewInstaller's, because the overlay offered
    them and the drive already had them - reading its own log would have said
    "skipped guigfx.library: already exists". A record that names a file this
    build did not write is worse than no record: it is an instruction to
    delete somebody else's.

    A drawer is named as one line only when this build made it, so that
    everything inside it came from the package. Otherwise - a system drawer,
    or one the drive already had - the files are named one by one.
    """
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for pair in pairs:
        source_text, destination = pair
        source = Path(source_text)
        key = credit.get(pair, "")
        package = packages.CATALOGUE_BY_KEY.get(key)
        if package is not None:
            label = package.label
        else:
            #  An overlay the user added themselves, or one the setup made.
            label = key or "Added by this build"
        where = str(destination).strip("/")
        written, made_the_drawer = (landings or {}).get(pair, (None, False))
        if written is None:
            #  No record of the copy: describe the intent, and never as a
            #  whole drawer, because nothing here knows what else is in it.
            written = _intended_paths(source, where)
            made_the_drawer = False
        if not written:
            continue
        own_drawer = (bool(where) and made_the_drawer
                      and where.lower() not in SYSTEM_DRAWERS)
        if own_drawer:
            landed = [f"{where}  ; whole drawer, {len(written)} "
                      f"file{'' if len(written) == 1 else 's'}"]
        elif len(written) > MANIFEST_FILE_LIMIT:
            landed = [f"{where or ':'}  ; {len(written)} files added here, "
                      f"too many to list"]
        else:
            landed = list(written)
        if label not in grouped:
            grouped[label] = []
            order.append(label)
        grouped[label] += landed
    return [(label, grouped[label]) for label in order]


def _intended_paths(source: Path, where: str) -> list[str]:
    """Where an overlay would land, for a caller that did not watch it copy."""
    if source.is_file():
        return [f"{where}/{source.name}" if where else source.name]
    if not source.is_dir():
        return []
    out = []
    for child in sorted(source.rglob("*")):
        if not child.is_file():
            continue
        relative = child.relative_to(source).as_posix()
        out.append(f"{where}/{relative}" if where else relative)
    return out


def _manifest_text(config: "BuildConfig", pairs: list[tuple[str, str]],
                   credit: dict[tuple[str, str], str],
                   landings: dict | None = None) -> str:
    """The card's own record of what this build added to it.

    AmigaOS has no uninstaller and Commodore's Installer never had a removal
    facility, so anything put on a drive is removed by hand or not at all.
    Nothing here goes through Installer - the files are copied into place
    directly - so no Installer log exists to work from either.  This file is
    that record: every path this build wrote, under the package that asked
    for it, in a form that can be read on the Amiga with ``Type`` and acted
    on with ``Delete``.
    """
    entries = _manifest_entries(pairs, credit, landings)
    startup = _package_startup_lines(config)
    if not entries and not startup:
        return ""
    when = datetime.datetime.now().strftime("%d-%b-%Y %H:%M")
    out = [
        "; What the PiStorm imager put on this card.",
        f"; Written {when}.",
        ";",
        "; AmigaOS has no uninstaller, and none of this was installed through",
        "; Commodore's Installer, so there is no install log to undo. To take",
        "; a package off this card, delete the paths listed under it:",
        ";",
        ";     Delete SYS:<path>          for a file",
        ";     Delete SYS:<path> ALL      for a drawer",
        ";",
        "; Paths are relative to the drive this file is on.",
    ]
    for label, landed in entries:
        out.append("")
        out.append(f"; {label}")
        out += landed
    if startup:
        out.append("")
        out.append("; These lines were added to the end of S:User-Startup,")
        out.append("; and should come out with the software that needs them.")
        out += [f";     {line}" for line in startup]
    return "\n".join(out) + "\n"


def _amiga_bytes(text: str) -> bytes:
    """Encode text for the Amiga, whatever a file name turns out to contain.

    File names come off the host with ``surrogateescape``: a byte that is not
    valid UTF-8 arrives as a lone surrogate, and MUI's ``Locale/Catalogs``
    holds several - ``fran\udce7ais`` among them.  A plain latin-1 encode
    raises on those, which ended an hour-long build at its very last step.
    Encoding them back through ``surrogateescape`` restores the original byte,
    which is the byte the Amiga had in the first place.

    Anything genuinely outside latin-1 - a name that really is UTF-8, from a
    host folder rather than an Amiga archive - has no Amiga representation at
    all, so it becomes a question mark rather than an exception.
    """
    try:
        return text.encode("latin-1", "surrogateescape")
    except UnicodeEncodeError:
        cleaned = "".join(
            character if (ord(character) < 256
                          or 0xDC80 <= ord(character) <= 0xDCFF) else "?"
            for character in text)
        return cleaned.encode("latin-1", "surrogateescape")


def _write_manifest(volume, config: "BuildConfig",
                    pairs: list[tuple[str, str]],
                    credit: dict[tuple[str, str], str],
                    progress: Progress, landings: dict | None = None) -> None:
    """Write that record onto the drive the machine boots from."""
    try:
        _write_manifest_now(volume, config, pairs, credit,
                            progress, landings)
    except Exception as error:                              # noqa: BLE001
        #  This file is a convenience, written at the very end of a build
        #  that takes an hour.  Nothing about it is worth losing that build
        #  for, and the first version of it did exactly that: a latin-1
        #  encode raised on a French catalogue's name and took the card with
        #  it, unclosed and unformatted.
        progress.log(f"  WARNING: the record of what was installed could not "
                     f"be written ({error}). The card is unaffected.")


def _write_manifest_now(volume, config: "BuildConfig",
                        pairs: list[tuple[str, str]],
                        credit: dict[tuple[str, str], str],
                        progress: Progress,
                        landings: dict | None = None) -> None:
    """Write it, and let anything that goes wrong reach the caller."""
    body = _manifest_text(config, pairs, credit, landings)
    if not body:
        return
    folder = volume.makedirs("S")
    name = MANIFEST_PATH.rpartition("/")[2]
    if volume._entry_exists(folder, name) is not None:
        #  An earlier build's manifest, carried in with an imported drive.
        #  It describes a card that no longer exists, so it is not left to
        #  be read as though it described this one.
        progress.log(f"  S:{name} already exists and describes an earlier "
                     f"build; this build's record could not replace it")
        return
    volume.write_file(folder, name, _amiga_bytes(body),
                      check_existing=False)
    lines = sum(1 for line in body.splitlines() if not line.startswith(";"))
    progress.log(f"  S:{name} written: {lines} path(s) this build added")


def _landing_paths(pairs: list[tuple[str, str]]) -> list[str]:
    """Where a set of overlays will put single files on the drive.

    Only files: a whole drawer is merged into whatever is already there, and
    refusing one during the copy would take out the drive's own contents
    along with it.
    """
    out: list[str] = []
    for source, destination in pairs:
        path = Path(source)
        if path.is_file():
            out.append(f"{destination}/{path.name}" if destination
                       else path.name)
        elif destination:
            #  A drawer going onto the card needs its name free. ClassicWB
            #  keeps Visage as a *file* in Utilities, and this build wants a
            #  drawer of that name there - which ended an hour-long build
            #  outright. Only a file can ever be refused by this: the copy
            #  asks about files and never about drawers, so a drawer of the
            #  same name is merged into as before.
            out.append(destination)
    return out


def _boot_drive_is_filled(config: "BuildConfig") -> bool:
    """Whether the drive the machine boots from is filled from elsewhere."""
    return any(spec.bootable and (spec.content_hdf or spec.content_folder)
               for spec in config.amiga_partitions)


def _package_overlays(config: "BuildConfig", existing: list[tuple[str, str]],
                      progress: Progress,
                      credit: dict[tuple[str, str], str] | None = None
                      ) -> list[tuple[str, str]]:
    """Resolve the chosen software into files to copy onto the drive.

    Each package is fetched from its publisher - Aminet, or the project that
    makes it - and cached between builds.

    ``credit``, if given, is filled in with which package each pair came from,
    so the card can be told afterwards what was put on it and by whom.
    """
    if not config.package_keys:
        return []
    from . import machines
    chipset = (machines.Chipset(config.package_chipset)
               if config.package_chipset else machines.Chipset.AGA)
    display = (machines.Display(config.package_display)
               if config.package_display else machines.Display.NATIVE)
    progress.step("Adding the software you chose")
    by_package = packages.overlays_by_package(
        config.package_keys, chipset=chipset, display=display,
        progress=progress)
    resolved = [pair for _key, pairs in by_package for pair in pairs]
    if credit is not None:
        for key, pairs in by_package:
            for pair in pairs:
                credit.setdefault(pair, key)
    #  The quick setup resolves the same packages while it assembles the
    #  configuration, so those pairs may already be on the partition. Adding
    #  them twice would copy every file twice; leaving them out of this list
    #  would drop them entirely for a build driven from the pages, where
    #  nothing resolved them earlier.
    already = {(source, destination) for source, destination in existing}
    out = [pair for pair in resolved if pair not in already]
    repositories = _igame_repositories(config, progress)
    if credit is not None:
        for pair in repositories:
            credit.setdefault(pair, "igame")
    out += repositories
    return out


def _igame_repositories(config: "BuildConfig",
                        progress: Progress) -> list[tuple[str, str]]:
    """Tell iGame which drawers on this card hold games.

    iGame keeps that list in ``repos.prefs``, and its Aminet archive ships
    none: installed cleanly it comes up with nothing to scan and no way to
    know where the games went, so "Scan Repositories" finds nothing and the
    list stays empty. The build knows exactly which drives it filled, so it
    says so.

    Nothing is guessed. A drive is named only if this build put content on
    it, and the WHDLoad drawer inside is named only if it is really there -
    pointing iGame at a drawer that does not exist is how the donor's own
    list behaved, and it is no better written by us.
    """
    if "igame" not in packages.expand(config.package_keys or []):
        return []
    boot = {spec.name.upper() for spec in config.amiga_partitions
            if spec.bootable}
    lines: list[str] = []
    for spec in config.amiga_partitions:
        if spec.name.upper() in boot or not spec.content_folder:
            continue
        volume = (spec.volume_name or spec.name).strip()
        if not volume:
            continue
        folder = Path(spec.content_folder)
        inside = ""
        try:
            inside = next((child.name for child in folder.iterdir()
                           if child.is_dir()
                           and child.name.lower() == "whdload"), "")
        except OSError:
            inside = ""
        lines.append(f"{volume}:{inside}" if inside else f"{volume}:")
    if not lines:
        return []
    written = Path(tempfile.mkdtemp(prefix="pistorm-igame-")) / "repos.prefs"
    written.write_text("\n".join(lines) + "\n")
    progress.log("iGame will scan: " + ", ".join(lines))
    return [(str(written), "Programs/iGame")]


def _drawer_exists(volume, destination: str) -> bool:
    """Whether the drive already has this drawer, before an overlay makes it.

    It decides whether a drawer may be named in the record as one thing to
    delete. A drawer this build created holds nothing but what this build put
    in it; a drawer that was already there - ClassicWB's own ``System/MUI``,
    which our MUI merges 56 files into and skips 339 - does not, and naming it
    would offer up the drive's own files under a package's name.
    """
    if not destination:
        return True
    block = volume.root
    for name in destination.strip("/").split("/"):
        found = volume._entry_exists(block, name)
        if found is None:
            return False
        #  FFS hands back an Entry and PFS3 a (anode, is_dir) pair; both take
        #  their own number back as a parent.
        if isinstance(found, tuple):
            block, is_dir = found[0], bool(found[1])
        else:
            block, is_dir = found.block, found.is_dir
        if not is_dir:
            return False
    return True


def _apply_overlays(volume, spec: AmigaPartitionSpec, fixer,
                    progress: Progress,
                    landings: dict | None = None) -> None:
    """Copy extra files or folders on top of a filled partition.

    ``landings``, if given, is filled in with what each overlay really put on
    the drive: the paths written, and whether this build made the drawer they
    went into.
    """
    for source_text, destination in spec.overlays:
        pair = (source_text, destination)
        source = Path(source_text)
        if not source.exists():
            progress.log(f"  overlay missing, skipped: {source}")
            continue
        if source.is_dir():
            made_the_drawer = not _drawer_exists(volume, destination)
            written: list[str] = []
            try:
                copied, _renamed = amigaos.install_tree(
                    volume, source, destination, progress, compat=fixer,
                    merge=True, written=written)
            except (amigafs.AmigaFsError, pfs3.Pfs3Error) as error:
                #  One package colliding with the drive is not a reason to
                #  throw away a card that took an hour to build. Say which,
                #  and go on with the rest.
                progress.log(f"  WARNING: {source.name} could not be "
                             f"installed into {destination or ':'} - {error}. "
                             f"Everything else is unaffected.")
                continue
            if landings is not None and written:
                already, made = landings.get(pair, ([], made_the_drawer))
                landings[pair] = (already + written, made and made_the_drawer)
            progress.log(f"  overlay: {source.name}/ -> {destination or ':'} "
                         f"({copied} files)")
        else:
            parent = volume.makedirs(destination) if destination else volume.root
            #  A tree copied as an overlay goes through the compatibility pass;
            #  a single file did not, so a package that is one file - WHDLoad's
            #  own preferences among them - was written exactly as found.
            if volume._entry_exists(parent, source.name) is not None:
                #  Whatever is there came from the floppies or an earlier
                #  package and is no worse than this copy.  A tree copy has
                #  always skipped in this situation; a single file raised, and
                #  ended the whole build over one duplicate file.
                progress.log(f"  overlay: {source.name} already present in "
                             f"{destination or ':'}, left as it is")
                continue
            data = source.read_bytes()
            if fixer is not None:
                relative = amigaos.landed_path(destination, source.name)
                data = fixer.offer(relative, data)
                #  A tree's files have always been able to be refused - an
                #  emulator's monitor is kept back and written out again under
                #  the name this machine's board uses. A single file could not
                #  be, so it went on the card unchanged and under the old name.
                if fixer.skip(relative):
                    continue
            volume.write_file(parent, source.name, data, check_existing=True)
            if landings is not None:
                landings[pair] = ([amigaos.landed_path(destination,
                                                       source.name)], False)
            progress.log(f"  overlay: {source.name} -> {destination or ':'}")


def _give_drawers_icons(volume, spec: AmigaPartitionSpec,
                        config: "BuildConfig", progress: Progress) -> None:
    """Make the drawers this build created visible on Workbench.

    Copying a package into ``Programs`` or ``Internet`` creates the drawer but
    not its icon, and a drawer with no icon does not appear on Workbench at
    all.  Every browser and launcher the user ticked went onto the card and
    then could not be found from the desktop.

    The drawers to cover are taken from where the packages were actually put,
    rather than a fixed list, so a package added later is covered by having a
    destination at all.  ``Storage`` is added because the real Commodore
    installer creates that drawer *and* its icon, while installing from the
    ADFs here creates only the drawer.
    """
    wanted: list[str] = ["Storage"]
    for _source, destination in spec.overlays:
        path = str(destination).strip("/")
        while path:
            if path not in wanted:
                wanted.append(path)
            path = path.rpartition("/")[0]

    #  Where to find real drawer icons: the Workbench disks, which is the
    #  only source left now that no icon set is shipped.
    sources: list[Path] = []
    if config.adf_folder:
        borrowed = Path(tempfile.mkdtemp(prefix="pistorm-drawer-icon-"))
        if amigaos.drawer_icon_from_disks(config.adf_folder, borrowed):
            sources.append(borrowed)

    written = amigaos.ensure_drawer_icons(volume, wanted, sources, progress)
    if written:
        progress.log(f"{written} drawer(s) given an icon")


def _make_fixer(config: BuildConfig, progress: Progress) -> "compat.Compatibility":
    """The compatibility pass, set up the same way wherever it is used."""
    fixer = compat.Compatibility(progress, enabled=config.fix_compatibility,
                                 rtg=config.rtg_display,
                                 native=config.native_display,
                                 workbench_on_rtg=config.workbench_on_rtg)
    if "picasso96" in (config.package_keys or ()):
        fixer.expect_picasso()
    #  What each volume will be filled from, so a games list can be checked
    #  against what is actually going onto the card.
    for spec in config.amiga_partitions:
        if spec.content_folder:
            name = (spec.volume_name or spec.name).strip().upper()
            fixer.content[name] = (Path(spec.content_folder),
                                   tuple(spec.exclude or ()))
    if config.spare_files_folder:
        found = fixer.add_spares(config.spare_files_folder)
        if found:
            progress.log(f"{found} replacement file(s) available from "
                         f"{config.spare_files_folder}")
    return fixer


def _format_empty_partitions(config: BuildConfig, handle,
                             amiga: mbr.MbrPartition, table: rdb.Rdb,
                             progress: Progress) -> None:
    """Format the partitions nothing else is going to.

    A partition with no content was left as raw sectors, and AmigaOS shows
    that as NDOS: you ask for a drive called Work, and get an icon that has to
    be formatted by hand before it can be used.  There is no sense in that for
    a file system this tool creates anyway - and on a PFS3 drive it is worse
    than an inconvenience, because the Amiga's own Format has no idea what
    PFS3 is unless the handler is already running.

    Anything already carrying a file system is left alone, so this cannot
    touch a drive imported from somewhere else.
    """
    by_name = {p.drive_name.upper(): p for p in table.partitions}
    filled = {spec.name.upper() for spec in config.amiga_partitions
              if spec.content_folder or spec.content_hdf}
    boot = next((p for p in table.partitions if p.bootable), None)
    for spec in config.amiga_partitions:
        name = spec.name.upper()
        partition = by_name.get(name)
        if partition is None or name in filled:
            continue
        #  The boot drive belongs to the AmigaOS install, which formats it
        #  itself and would have its work thrown away here.
        if (config.install_amigaos and boot is not None
                and name == boot.drive_name.upper()):
            continue
        dostype = partition.dostype
        if not amigafs.is_ffs(dostype) and dostype not in (rdb.DOSTYPE_PFS3,
                                                           rdb.DOSTYPE_PDS3):
            continue
        offset = partition.byte_offset(table.geometry, amiga.start_bytes)
        handle.seek(offset)
        signature = handle.read(4)
        if signature[:3] == b"DOS" or signature == b"PFS\x01":
            continue
        label = spec.volume_name or partition.drive_name
        progress.step(f"Formatting {partition.drive_name}")
        volume = amigaos.make_volume(handle, offset,
                                     partition.blocks(table.geometry),
                                     label, dostype)
        volume.close()
        progress.log(f'{partition.drive_name} formatted as '
                     f'{rdb.dostype_name(dostype)}, named "{label}"')


def _check_the_system_can_boot(config: BuildConfig, progress: Progress) -> None:
    """Refuse a card whose system drive brings no operating system.

    A drive can boot and still have no Workbench: ClassicWB's carries no
    C:LoadWB, no C:IPrefs and no Version, because those are Commodore's. Put
    it on a card without the disks and the card stops at a Shell saying
    "C:Version: Unknown command".
    """
    from . import presets                         # noqa: PLC0415 - circular

    if config.install_amigaos:
        return
    for spec in config.amiga_partitions:
        if not spec.bootable or not spec.content_hdf:
            continue
        if presets.inspect_image_system(
                spec.content_hdf, spec.content_hdf_partition).needs_floppies:
            raise RuntimeError(
                f"{Path(spec.content_hdf).name} brings no Workbench of its "
                f"own - it has no C:LoadWB - so a card made from it alone "
                f"cannot boot. Choose the Workbench disk images as well and "
                f"they will fill in what it does not have.")


#  Packages that only work if a line goes into S:Startup-Sequence, and the
#  file each of them installs. A distribution that carries its own boot script
#  has that script written out verbatim, so the line never lands - and the
#  library then sits in LIBS: where something else can open it at the wrong
#  moment. PeterK's icon.library did exactly that: DefIcons asks for
#  icon.library 44, which the ROM's v40 cannot answer, so AmigaOS loaded the
#  disk copy after Workbench had already started on the ROM one, and the card
#  boot-looped on real hardware.
NEEDS_THE_BOOT_SCRIPT = {"iconlib": "icon.library"}


def _drop_what_needs_the_boot_script(pairs: list[tuple[str, str]],
                                     config: "BuildConfig", fixer,
                                     progress: Progress
                                     ) -> list[tuple[str, str]]:
    """Leave out software whose startup line cannot be installed here."""
    if not getattr(fixer, "writes_its_own_startup", False):
        return pairs
    chosen = set(packages.expand(config.package_keys or []))
    unusable = {name.lower() for key, name in NEEDS_THE_BOOT_SCRIPT.items()
                if key in chosen}
    if not unusable:
        return pairs
    kept = [pair for pair in pairs
            if Path(pair[0]).name.lower() not in unusable]
    for key, name in NEEDS_THE_BOOT_SCRIPT.items():
        if key in chosen:
            progress.log(
                f"  WARNING: {packages.CATALOGUE_BY_KEY[key].label} is being "
                f"left out. It only works when a line can be added to "
                f"S:Startup-Sequence, and this drive brings its own boot "
                f"script. Installed anyway, {name} sits where something else "
                f"opens it at the wrong moment - which boot-looped a card.")
    return kept


def _follow_launchers(spec: AmigaPartitionSpec, reader, source: Path | None,
                      progress: Progress) -> list[str]:
    """Leave out what an excluded launcher was the only thing running.

    A title can be a few bytes naming the program that runs it. Leaving the
    launcher out and keeping what it names wastes the very space the exclusion
    was for, on something nothing can now reach.
    """
    if not spec.exclude:
        return list(spec.exclude or [])
    try:
        if source is not None:
            names = [p.name for p in source.iterdir()]
            offered = [c.path for c in content.discover(source)]

            def read(name: str):
                path = source / name
                return path.read_bytes() if path.is_file() else None
        else:
            top = reader.listdir()
            names = [e.name for e in top]
            offered = [c.path for c in content.discover_volume(reader)]
            by_name = {e.name.lower(): e for e in top}

            def read(name: str):
                entry = by_name.get(name.lower())
                return None if entry is None or entry.is_dir \
                    else reader.read_file(entry)
    except Exception as error:                   # noqa: BLE001 - not fatal
        progress.log(f"  could not check what the exclusions run: {error}")
        return list(spec.exclude)

    extra = content.followed(spec.exclude, read, names, offered)
    for name in extra:
        progress.log(f"  {name} left out as well: only something you removed "
                     f"ran it")
    return list(spec.exclude) + extra


def _install_content(config: BuildConfig, handle, amiga: mbr.MbrPartition,
                     table: rdb.Rdb, progress: Progress) -> None:
    """Fill partitions that were given a host directory or overlays."""
    by_name = {p.drive_name.upper(): p for p in table.partitions}
    for spec in config.amiga_partitions:
        if not spec.content_folder and not spec.content_hdf:
            continue
        partition = by_name.get(spec.name.upper())
        if partition is None:
            progress.log(f"No partition called {spec.name}; skipping its content")
            continue
        capacity = partition.size_bytes(table.geometry)
        offset = partition.byte_offset(table.geometry, amiga.start_bytes)
        fixer = _make_fixer(config, progress)
        #  The software goes on the drive the machine boots from, and it is
        #  resolved before the drive is filled so it can take the place of an
        #  older copy already in the image - if that is what was asked for.
        credit: dict[tuple[str, str], str] = {}
        extra = (_package_overlays(config, list(spec.overlays), progress,
                                   credit)
                 if spec.bootable else [])
        extra = _drop_what_needs_the_boot_script(extra, config, fixer,
                                                 progress)
        if extra and config.replace_older_software:
            fixer.displace(_landing_paths(extra))
        if spec.bootable:
            #  See above: an earlier build's record is not left standing in
            #  front of this one's.
            fixer.displace([MANIFEST_PATH])
        #  The drive brings its own S:User-Startup and this file system never
        #  overwrites, so it is held back and written out again below with
        #  the packages' lines after it - otherwise FBlit, FText and Birdie
        #  go onto the card and are never run.
        if spec.bootable and _package_startup_lines(config):
            fixer.keep_user_startup()

        if spec.content_hdf:
            from . import presets                 # noqa: PLC0415 - circular
            #  Only when the Workbench disks are being installed as well:
            #  its installer exists to copy Commodore's files off a floppy,
            #  and taking it away without supplying them leaves a card that
            #  cannot boot at all - which is worse than one that asks.
            if (spec.bootable and config.install_amigaos
                    and presets.finishable_install(
                        spec.content_hdf, spec.content_hdf_partition)):
                #  A distribution that would otherwise boot into its own
                #  installer: it is carried out here instead.
                fixer.finish_classicwb_install()
            reader, label = amigaos.open_amiga_volume(spec.content_hdf,
                                                      spec.content_hdf_partition)
            progress.step(f"Filling {partition.drive_name} from {label}")
            volume = amigaos.make_volume(handle, offset,
                                         partition.blocks(table.geometry),
                                         spec.volume_name or partition.drive_name,
                                         partition.dostype)
            copied, skipped = amigaos.copy_volume(
                reader, volume, "", progress, skip_existing=False,
                compat=fixer,
                exclude=_follow_launchers(spec, reader, None, progress))
            try:
                reader.f.close()
            except Exception:  # noqa: BLE001
                pass
            progress.log(f"{copied} files copied"
                         + (f", {skipped} left out" if skipped else ""))
        elif spec.content_folder:
            source = Path(spec.content_folder)
            payload, count = amigaos.tree_size(source)
            progress.step(f"Filling {partition.drive_name} from {source.name}")
            progress.log(f"{count} files, {human_size(payload)} -> "
                         f"{partition.drive_name} ({human_size(capacity)})")
            if payload > capacity * 0.97:
                raise RuntimeError(
                    f"{source.name} needs {human_size(payload)} but "
                    f"{partition.drive_name} is only {human_size(capacity)}.")
            volume = amigaos.make_volume(handle, offset,
                                         partition.blocks(table.geometry),
                                         spec.volume_name or partition.drive_name,
                                         partition.dostype)
            copied, renamed = amigaos.install_tree(
                volume, source, "", progress, compat=fixer,
                exclude=_follow_launchers(spec, None, source, progress))
            progress.log(f"{copied} files copied"
                         + (f", {renamed} renamed for AmigaDOS" if renamed else ""))
        else:
            #  Overlay-only partitions are handled where they were installed.
            continue
        if spec.bootable and config.install_amigaos:
            _add_missing_system(config, volume, fixer, progress)
        if extra:
            spec = dataclasses.replace(spec,
                                       overlays=list(spec.overlays) + extra)
        #  The drive and the disks have had their turn; what follows is the
        #  packages writing the very files those were refused for.
        fixer.stop_displacing()
        landings: dict = {}
        _apply_overlays(volume, spec, fixer, progress, landings)
        if spec.bootable:
            #  Everything the floppy install does once the files are on:
            #  without these the software went on and had no icons, and
            #  nothing that needed a startup line ever ran.
            _give_drawers_icons(volume, spec, config, progress)
            _write_user_startup(volume, config, progress,
                                fixer.kept_user_startup)
            _write_manifest(volume, config, list(spec.overlays), credit,
                            progress, landings)
            #  Only the drive the machine boots from: Games and Demos were
            #  each being given their own copy of the display-switching
            #  scripts, which belong in the system drive's S: and nowhere.
            fixer.finish(volume, progress)
        volume.close()
        progress.log(fixer.summary())


def _write_partition_table(handle, config: BuildConfig, total_size: int,
                           progress: Progress) -> tuple[mbr.MbrPartition, mbr.MbrPartition]:
    progress.step("Creating the partition table")
    total_sectors = total_size // SECTOR
    boot_start = DEFAULT_BOOT_START
    boot_sectors = align_up(config.boot_size, MIB) // SECTOR
    amiga_start = align_up(boot_start + boot_sectors, MIB // SECTOR)
    if amiga_start >= total_sectors:
        raise RuntimeError("the boot partition does not leave room for an Amiga partition")
    amiga_sectors = total_sectors - amiga_start

    boot = mbr.MbrPartition(0, 0x80, mbr.TYPE_FAT32_LBA, boot_start, boot_sectors)
    amiga = mbr.MbrPartition(1, 0x00, mbr.TYPE_AMIGA, amiga_start, amiga_sectors)
    #  Wipe any stale table and filesystem signatures at the head of the card.
    handle.seek(0)
    handle.write(b"\0" * (DEFAULT_BOOT_START * SECTOR))
    mbr.write_table(handle, [boot, amiga], disk_id=0x50495354)  # 'PIST'
    progress.log(f"1: FAT32 boot  {human_size(boot.size_bytes)} at sector {boot_start}")
    progress.log(f"2: Amiga 0x76  {human_size(amiga.size_bytes)} at sector {amiga_start}")
    return boot, amiga


def _write_image(config: BuildConfig, handle, target_size: int,
                 progress: Progress) -> None:
    source = imgsrc.inspect(config.source_image)
    progress.step(f"Writing {source.path.name} to the card")
    progress.log(source.description)
    needed = source.write_size
    if needed and needed > target_size:
        raise RuntimeError(
            f"The image needs {human_size(needed)} but the target is only "
            f"{human_size(target_size)}."
        )
    stream, proc = imgsrc.open_stream(source)
    try:
        handle.seek(0)
        written = copy_stream(stream, handle, needed, progress)
        progress.log(f"Wrote {human_size(written)}")
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
        if proc is not None:
            proc.wait()
    handle.flush()
    os.fsync(handle.fileno())


#  File system signatures that can appear at block 0 of a bare, single-partition
#  hard disk image (one with no Rigid Disk Block in front of it).
BARE_SIGNATURES = {
    b"DOS\x00": rdb.DOSTYPE_OFS, b"DOS\x01": rdb.DOSTYPE_FFS,
    b"DOS\x02": rdb.DOSTYPE_OFS_INTL, b"DOS\x03": rdb.DOSTYPE_FFS_INTL,
    b"DOS\x04": rdb.DOSTYPE_OFS_DC, b"DOS\x05": rdb.DOSTYPE_FFS_DC,
    b"PFS\x03": rdb.DOSTYPE_PFS3, b"PDS\x03": rdb.DOSTYPE_PDS3,
    b"SFS\x00": rdb.DOSTYPE_SFS0,
}


@dataclasses.dataclass(frozen=True)
class Drive:
    """One Amiga drive inside an image, described well enough to choose it."""

    name: str                       # the device name, DH0 and so on
    volume: str                     # the label Workbench shows, if readable
    size: int                       # bytes
    filesystem: str                 # PFS3, FFS-INTL, ...
    bootable: bool

    @property
    def whole_image(self) -> bool:
        """A bare file system with no partition table: the file is the drive."""
        return not self.name

    @property
    def label(self) -> str:
        """What to show in a list: the drive, its volume and how big it is."""
        parts = [self.name or "The whole image"]
        if self.volume and self.volume.upper() != self.name.upper():
            parts.append(f'"{self.volume}"')
        parts.append(human_size(self.size))
        parts.append(self.filesystem)
        if self.bootable:
            parts.append("bootable")
        return "  -  ".join((parts[0], ", ".join(parts[1:])))


def list_drives(path: str | Path) -> list[Drive]:
    """The Amiga drives an image holds, for picking one to import.

    Reading the volume label means opening each file system, which can fail on
    a drive that was never formatted; that is reported as a drive with no
    label rather than losing the whole list.
    """
    try:
        handle = open(path, "rb")
    except OSError:
        return []
    with handle:
        located = find_rdb(handle)
        if located is None:
            #  No partition table.  A bare file system is still one drive -
            #  ClassicWB and plenty of older .hdf files are exactly this - and
            #  answering "nothing here" would leave the caller thinking no
            #  image had been chosen at all.
            try:
                volume, _label = amigaos.open_amiga_volume(path)
            except Exception:                     # noqa: BLE001 - best effort
                return []
            dostype = ("PFS3" if isinstance(volume, pfs3.Pfs3Volume)
                       else "FFS/OFS")
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
            return [Drive("", getattr(volume, "name", ""), size, dostype, False)]
        base, table = located
        drives: list[Drive] = []
        for part in table.partitions:
            volume = ""
            try:
                offset = part.byte_offset(table.geometry, base)
                if part.dostype in (rdb.DOSTYPE_PFS3, rdb.DOSTYPE_PDS3):
                    volume = pfs3.Pfs3Volume(handle, offset).name
                else:
                    #  FFS puts its root block in the middle of the partition,
                    #  so the size has to be the partition's.  Left to work it
                    #  out from the file, a drive inside a card image lands on
                    #  the wrong block and reads back as an empty volume.
                    volume = amigafs.Volume(handle, offset,
                                            part.blocks(table.geometry),
                                            part.reserved_blocks).name
            except Exception:                     # noqa: BLE001 - best effort
                volume = ""
            drives.append(Drive(part.drive_name, volume,
                                part.size_bytes(table.geometry),
                                part.dostype_name, part.bootable))
        return drives


#  MBR partition types worth naming when an image holds no Amiga drive.
LINUX_PARTITION = 0x83
LINUX_SWAP = 0x82


def why_no_drives(path: str | Path) -> str:
    """Explain an image that offers no Amiga drive, for the drive chooser.

    "No Amiga drive found" is true of a PiMiga download and thoroughly
    unhelpful: it is the file everyone reaches for first, and the reason it
    holds no Amiga drive - its drives are folders inside a Linux root
    partition - is the one thing the user needs told.
    """
    try:
        handle = open(path, "rb")
    except OSError as error:
        return f"Cannot read this file: {error}"
    with handle:
        if find_rdb(handle) is not None:
            return ""
        try:
            parts = [p for p in mbr.read_table(handle) if not p.empty]
        except (ValueError, OSError):
            parts = []
        if any(p.type_id == mbr.TYPE_AMIGA for p in parts):
            return ("This card image has an Amiga partition, but no Rigid "
                    "Disk Block could be read inside it.")
        if any(p.type_id in (LINUX_PARTITION, LINUX_SWAP) for p in parts):
            return ("This is a Linux system image, not an Amiga drive. A "
                    "PiMiga download is a Raspberry Pi system that runs an "
                    "emulator, and its Amiga drives are ordinary folders "
                    "inside its Linux root partition - mount that partition "
                    "and point the PiMiga folder source at disks/ instead.")
        if parts:
            return ("This image has a partition table, but none of it is an "
                    "Amiga drive.")
        return ("No partition table and no Amiga file system: this file "
                "cannot be used as an Amiga drive.")


def find_rdb(handle) -> tuple[int, "rdb.Rdb"] | None:
    """Locate a Rigid Disk Block in an open image.

    Handles both shapes we care about: a bare ``.hdf`` whose block 0 is the RDB,
    and a whole card image (a PiMiga download, or a backup of a card) where the
    RDB lives at the start of the 0x76 partition.
    """
    try:
        return 0, rdb.Rdb.read(handle, 0)
    except (ValueError, OSError):
        pass
    try:
        parts = mbr.read_table(handle)
    except (ValueError, OSError):
        return None
    for part in parts:
        if part.type_id != mbr.TYPE_AMIGA or part.empty:
            continue
        try:
            return part.start_bytes, rdb.Rdb.read(handle, part.start_bytes)
        except (ValueError, OSError):
            continue
    return None


@dataclasses.dataclass
class HdfInfo:
    """What we learned about an Amiga hard disk image before writing it."""

    path: Path
    size: int
    table: "rdb.Rdb | None"
    compression: str = "none"
    bare_dostype: int | None = None   # set when the file is a single partition
    source_offset: int = 0            # where the Amiga drive starts in the file
    source_length: int = 0            # how much of the file to copy
    from_card_image: bool = False

    @property
    def description(self) -> str:
        if self.from_card_image:
            names = ", ".join(p.drive_name for p in self.table.partitions) \
                if self.table else "?"
            return (f"{self.path.name} - card image; taking its "
                    f"{human_size(self.source_length)} Amiga drive "
                    f"({names})")
        if self.table is None and self.bare_dostype is not None:
            return (f"{self.path.name} - {human_size(self.size)}, single "
                    f"{rdb.dostype_name(self.bare_dostype)} partition with no RDB "
                    f"(one will be created around it)")
        if self.table is None:
            return f"{self.path.name} - {human_size(self.size)}, no RDB found"
        names = ", ".join(p.drive_name for p in self.table.partitions)
        drivers = ", ".join(rdb.dostype_name(f.dostype) for f in self.table.filesystems)
        text = f"{self.path.name} - {human_size(self.size)}, partitions: {names or 'none'}"
        if drivers:
            text += f"; file systems in RDB: {drivers}"
        return text


def inspect_hdf(path: str | Path) -> HdfInfo:
    """Read the RDB out of an .hdf without reading the whole (often huge) file."""
    path = Path(path)
    source = imgsrc.inspect(path)
    size = source.write_size or path.stat().st_size
    table = None
    bare = None
    offset = 0
    length = size
    from_card = False
    if source.compression == "none":
        with open(path, "rb") as handle:
            located = find_rdb(handle)
            if located is not None:
                offset, table = located
                if offset:
                    from_card = True
                    #  Copy only the 0x76 partition, not the whole card.
                    parts = mbr.read_table(handle)
                    part = next(p for p in parts
                                if p.type_id == mbr.TYPE_AMIGA and not p.empty
                                and p.start_bytes == offset)
                    length = min(part.size_bytes, size - offset)
            else:
                try:
                    handle.seek(0)
                    bare = BARE_SIGNATURES.get(handle.read(4))
                except OSError:
                    bare = None
    return HdfInfo(path, size, table, source.compression, bare,
                   source_offset=offset, source_length=length,
                   from_card_image=from_card)


def geometry_dividing(blocks: int) -> rdb.Geometry:
    """Pick a drive geometry whose cylinder size divides ``blocks`` exactly.

    Wrapping a bare file system image means declaring a partition of precisely
    the same number of blocks: the image's own bitmap describes that many and no
    more, so a partition rounded up to the next cylinder would leave AmigaOS
    believing in blocks the file system knows nothing about.  Since RDB
    partitions must start and end on cylinder boundaries, we choose the
    geometry to fit the image rather than the other way round.
    """
    candidates = [(16, 128), (16, 63), (8, 32), (4, 32), (2, 32),
                  (1, 32), (1, 16), (1, 8), (1, 4), (1, 2), (1, 1)]
    for heads, sectors in candidates:
        if blocks % (heads * sectors) == 0:
            return rdb.Geometry(heads=heads, sectors=sectors)
    return rdb.Geometry(heads=1, sectors=1)


def _write_bare_hdf(config: BuildConfig, handle, amiga: mbr.MbrPartition,
                    info: HdfInfo, progress: Progress) -> None:
    """Write a single-partition image and build an RDB around it.

    Images like the ClassicWB ``System_*.hdf`` files are a bare file system with
    no partition table at all.  Emu68 hands the whole 0x76 partition to AmigaOS
    as a drive and looks for a Rigid Disk Block at its start, so such an image
    has to be moved aside and described by an RDB we generate.
    """
    dostype = info.bare_dostype or rdb.DOSTYPE_FFS_INTL
    blocks = info.size // rdb.BLOCK
    if info.size % rdb.BLOCK:
        raise RuntimeError(f"{info.path.name} is not a whole number of 512-byte blocks")

    geometry = geometry_dividing(blocks)
    cyl_blocks = geometry.cyl_blocks
    first_cyl = max(1, -(-RDB_RESERVED_BLOCKS // cyl_blocks))
    image_cyls = blocks // cyl_blocks
    data_offset = amiga.start_bytes + first_cyl * cyl_blocks * rdb.BLOCK
    needed = first_cyl * cyl_blocks * rdb.BLOCK + info.size

    progress.step("Wrapping the hard disk image in a Rigid Disk Block")
    progress.log(f"{info.path.name} holds a bare {rdb.dostype_name(dostype)} "
                 f"file system; creating an RDB around it")
    if needed > amiga.size_bytes:
        raise RuntimeError(
            f"The image plus its RDB needs {human_size(needed)} but the Amiga "
            f"partition is only {human_size(amiga.size_bytes)}."
        )

    name = config.amiga_partitions[0].name if config.amiga_partitions else "DH0"
    partition = rdb.Partition(
        drive_name=name, low_cyl=first_cyl, high_cyl=first_cyl + image_cyls - 1,
        dostype=dostype, bootable=True, boot_priority=0)
    table = rdb.Rdb(
        geometry=geometry,
        partitions=[partition],
        filesystems=_filesystem_drivers(config, [partition], progress),
        cylinders=first_cyl + image_cyls,
        rdb_blocks_hi=min(RDB_RESERVED_BLOCKS, first_cyl * cyl_blocks) - 1,
    )
    table.write(handle, amiga.start_bytes)
    for line in table.describe().splitlines():
        progress.log(line)

    progress.step("Writing the Amiga hard disk image")
    source = imgsrc.inspect(info.path)
    stream, proc = imgsrc.open_stream(source)
    try:
        handle.seek(data_offset)
        written = copy_stream(stream, handle, info.size, progress)
        progress.log(f"Wrote {human_size(written)} as {name}")
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
        if proc is not None:
            proc.wait()


def _write_hdf(config: BuildConfig, handle, amiga: mbr.MbrPartition,
               progress: Progress) -> None:
    """Stream an Amiga hard disk image into the 0x76 partition."""
    info = inspect_hdf(config.hdf_image)
    progress.step("Writing the Amiga hard disk image")
    progress.log(info.description)
    if info.table is None:
        if info.bare_dostype is None:
            raise RuntimeError(
                f"{info.path.name} has neither a Rigid Disk Block nor a "
                f"recognisable Amiga file system at its start, so it cannot be "
                f"used as an Amiga hard disk image."
            )
        _write_bare_hdf(config, handle, amiga, info, progress)
        return
    for line in info.table.describe().splitlines():
        progress.log(line)
    payload = info.source_length or info.size
    if payload > amiga.size_bytes:
        raise RuntimeError(
            f"The hard disk image needs {human_size(payload)} but the Amiga "
            f"partition is only {human_size(amiga.size_bytes)}. Use a larger card "
            f"or a smaller boot partition."
        )
    source = imgsrc.inspect(config.hdf_image)
    stream, proc = imgsrc.open_stream(source)
    try:
        if info.source_offset:
            progress.log(f"Skipping to the Amiga drive at "
                         f"{human_size(info.source_offset)} into the image")
            _skip(stream, info.source_offset, progress)
        handle.seek(amiga.start_bytes)
        written = copy_stream(stream, handle, payload, progress, limit=payload)
        progress.log(f"Wrote {human_size(written)} into the Amiga partition")
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
        if proc is not None:
            proc.wait()


def _build_hdf_output(config: BuildConfig, handle, size: int,
                      progress: Progress) -> None:
    """Produce a bare Amiga hard disk image: an RDB and its partitions, no MBR.

    The result is what WinUAE, FS-UAE and this tool's own import mode all call
    an ``.hdf`` - useful on its own, and the natural way to build an Amiga drive
    once and write it to several cards later.
    """
    progress.step("Creating the Amiga hard disk image")
    table = _build_rdb(config, size // SECTOR, progress)
    table.write(handle, 0)
    for line in table.describe().splitlines():
        progress.log(line)
    amiga = mbr.MbrPartition(0, 0, mbr.TYPE_AMIGA, 0, size // SECTOR)
    if config.install_amigaos and not _boot_drive_is_filled(config):
        _install_amigaos(config, handle, amiga, table, progress)
    if any(p.content_folder or p.content_hdf
                           for p in config.amiga_partitions):
        _install_content(config, handle, amiga, table, progress)
    _format_empty_partitions(config, handle, amiga, table, progress)
    check_and_repair(handle, 0, size, config, progress)


def _skip(stream, count: int, progress: Progress) -> None:
    """Advance a stream by ``count`` bytes, seeking when the stream allows it."""
    try:
        if stream.seekable():
            stream.seek(count)
            return
    except (AttributeError, OSError):
        pass
    remaining = count
    while remaining > 0:
        progress.check_cancelled()
        chunk = stream.read(min(4 * MIB, remaining))
        if not chunk:
            raise RuntimeError("the image ended before the Amiga drive began")
        remaining -= len(chunk)


def check_and_repair(handle, offset: int, capacity: int, config: BuildConfig,
                     progress: Progress) -> None:
    """Analyse the RDB now on the card and apply safe compatibility fixes."""
    try:
        table = rdb.Rdb.read(handle, offset)
    except (ValueError, OSError) as error:
        progress.log(f"Could not re-read the RDB to check it: {error}")
        return

    #  A written-in system keeps its own drivers, which are right for it; what
    #  it cannot know is which screen this machine is watched on.
    if config.patch_display:
        progress.step("Adapting the display setup on the card")
        postwrite.adapt_display(handle, offset, table, config.rtg_display,
                                progress)

    progress.step("Checking the Amiga drive for PiStorm compatibility")
    findings = hdfcheck.analyse(table, capacity)
    if not findings:
        progress.log("No compatibility problems found.")
        return
    for finding in findings:
        progress.log(f"  {finding}")
    progress.log(hdfcheck.summarise(findings))

    blocking = hdfcheck.blocking(findings)
    if not config.repair_rdb:
        if blocking:
            raise RuntimeError(
                "This image is not usable on Emu68: "
                + "; ".join(f.message for f in blocking))
        progress.log("Automatic repair is switched off; leaving the RDB as it is.")
        return

    donors = _filesystem_drivers(config, table.partitions, progress) \
        if config.pfs3_binary else []
    actions = hdfcheck.repair(table, capacity, donors)
    for action in actions:
        progress.log(f"  fixed: {action}")
    if actions:
        table.write(handle, offset)
        progress.log(f"Rewrote the RDB with {len(actions)} correction"
                     f"{'s' if len(actions) != 1 else ''}.")
    after = hdfcheck.analyse(table, capacity)
    fatal = hdfcheck.blocking(after, after_repair=True)
    if fatal:
        raise RuntimeError(
            "This image still cannot be used on Emu68: "
            + "; ".join(f.message for f in fatal))
    for finding in hdfcheck.unresolved(after):
        progress.log(f"STILL A PROBLEM: {finding.partition or 'drive'} "
                     f"{finding.message}")
    if hdfcheck.unresolved(after):
        progress.log("Supply the missing file system handler (a pfs3aio binary, "
                     "or another image containing one) and build again, or add "
                     "it from HDToolBox on the Amiga.")


def _expand(handle, config: BuildConfig, target_size: int, progress: Progress) -> None:
    """Grow the 0x76 partition into unused space and add a partition there.

    Growing an existing Amiga partition would leave its file system describing a
    smaller disk than it now occupies, so instead we hand the reclaimed space to
    a brand new partition that the user formats on the Amiga.  Nothing already
    on the card is touched.
    """
    parts = mbr.read_table(handle)
    amiga = _find_amiga_partition(parts)
    if amiga is None:
        progress.log("No 0x76 partition found; nothing to expand.")
        return
    total_sectors = target_size // SECTOR
    free_sectors = total_sectors - amiga.end_lba
    if free_sectors < 64 * MIB // SECTOR:
        progress.log(f"Only {human_size(free_sectors * SECTOR)} unused; not expanding.")
        return

    progress.step("Expanding the Amiga drive into unused space")
    try:
        table = rdb.Rdb.read(handle, amiga.start_bytes)
    except ValueError as error:
        progress.log(f"Cannot expand: {error}")
        return

    old_end = amiga.end_lba
    amiga.sector_count = total_sectors - amiga.start_lba
    mbr.write_table(handle, parts)
    progress.log(f"0x76 partition grown from {human_size((old_end - amiga.start_lba) * SECTOR)}"
                 f" to {human_size(amiga.size_bytes)}")

    geometry = table.geometry
    new_cylinders = amiga.size_bytes // geometry.cyl_bytes
    first_free = max((p.high_cyl for p in table.partitions), default=0) + 1
    if first_free >= new_cylinders:
        progress.log("No whole cylinders left over; partition table unchanged.")
        return

    specs = config.extra_partitions or []
    if not specs:
        progress.log("No extra partitions requested.")
        table.cylinders = new_cylinders
        table.write(handle, amiga.start_bytes)
        return

    table.cylinders = new_cylinders
    used_names = {p.drive_name.upper() for p in table.partitions}
    cursor = first_free
    added: list[rdb.Partition] = []
    for index, spec in enumerate(specs):
        if cursor >= new_cylinders:
            progress.log(f"No room left for {spec.name}; skipped.")
            continue
        name = spec.name.upper() or "DH1"
        if name in used_names:
            replacement = next((f"DH{n}" for n in range(10)
                                if f"DH{n}" not in used_names), None)
            if replacement is None:
                progress.log(f"No free device name for {name}; skipped.")
                continue
            progress.log(f"{name} is taken; using {replacement} instead")
            name = replacement
        last = index == len(specs) - 1
        if spec.size is None or last and spec.size is None:
            high = new_cylinders - 1
        else:
            cylinders = -(-spec.size // geometry.cyl_bytes)
            high = min(cursor + cylinders - 1, new_cylinders - 1)
        if high < cursor:
            progress.log(f"No room left for {name}; skipped.")
            continue
        partition = rdb.Partition(
            drive_name=name, low_cyl=cursor, high_cyl=high,
            dostype=rdb.parse_dostype(spec.dostype),
            bootable=spec.bootable, boot_priority=spec.boot_priority)
        table.partitions.append(partition)
        added.append(partition)
        used_names.add(name)
        cursor = high + 1

    #  Any handler these new partitions need must be in the RDB as well.
    needed = [p for p in added if p.dostype not in hdfcheck.ROM_DOSTYPES]
    if needed and config.pfs3_binary:
        for handler in _filesystem_drivers(config, added, progress):
            if handler.dostype not in {f.dostype for f in table.filesystems}:
                table.filesystems.append(handler)

    table.write(handle, amiga.start_bytes)
    for partition in added:
        progress.log(f"Added {partition.drive_name}: "
                     f"{human_size(partition.size_bytes(geometry))} "
                     f"{rdb.dostype_name(partition.dostype)} - format it on the Amiga")


# ------------------------------------------------------------------- entry


def run_build(config: BuildConfig, progress: Progress) -> None:
    for concern in config.concerns():
        #  Said before anything is written, and the build goes ahead: these
        #  are choices that work and probably were not meant.
        progress.log(f"NOTE: {concern}")
    problems = config.validate()
    if problems:
        raise RuntimeError("; ".join(problems))

    if config.target_is_device:
        device = next((d for d in devices.list_devices(only_removable=False)
                       if d.path == config.target), None)
        if device is None:
            raise RuntimeError(f"{config.target} is not a block device we can see")
        devices.check_writable(device)
        devices.unmount_all(device, progress.log)

    target_size = _target_size(config)
    progress.log(f"Target {config.target} - {human_size(target_size)}")

    workdir = Path(tempfile.mkdtemp(prefix="pistorm-imager-"))
    try:
        emu68_files: list[Path] = []
        emu68_root: Path | None = None
        if config.install_emu68:
            emu68_files, emu68_root = _prepare_emu68(config, workdir, progress)

        create_size = target_size if (config.mode in (BuildMode.FRESH, BuildMode.HDF)
                                      and not config.target_is_device) else None
        with _open_target(config, create_size) as handle:
            if config.output_hdf:
                _build_hdf_output(config, handle, target_size, progress)
                handle.flush()
                os.fsync(handle.fileno())
                progress.fraction(1.0)
                progress.step("Done")
                return

            if config.mode is BuildMode.IMAGE:
                _write_image(config, handle, target_size, progress)

            if config.mode in (BuildMode.FRESH, BuildMode.HDF):
                boot_part, amiga_part = _write_partition_table(
                    handle, config, target_size, progress)

                progress.step("Creating the FAT32 boot partition")
                boot_image = _make_boot_filesystem(boot_part.size_bytes, workdir, progress)
                with open(boot_image, "r+b") as boot_handle:
                    fs = Fat32(boot_handle)
                    _populate_boot(fs, config, emu68_files, emu68_root, progress)
                    boot_handle.flush()
                    os.fsync(boot_handle.fileno())
                progress.step("Copying the boot partition onto the card")
                with open(boot_image, "rb") as boot_handle:
                    handle.seek(boot_part.start_bytes)
                    copy_stream(boot_handle, handle, boot_part.size_bytes, progress)

                if config.mode is BuildMode.HDF:
                    _write_hdf(config, handle, amiga_part, progress)
                    check_and_repair(handle, amiga_part.start_bytes,
                                     amiga_part.size_bytes, config, progress)
                else:
                    progress.step("Writing the Amiga partition table (RDB)")
                    table = _build_rdb(config, amiga_part.sector_count, progress)
                    table.write(handle, amiga_part.start_bytes)
                    for line in table.describe().splitlines():
                        progress.log(line)
                    if config.install_amigaos \
                            and not _boot_drive_is_filled(config):
                        _install_amigaos(config, handle, amiga_part, table, progress)
                    if any(p.content_folder or p.content_hdf
                           for p in config.amiga_partitions):
                        _check_the_system_can_boot(config, progress)
                        _install_content(config, handle, amiga_part, table, progress)
                    _format_empty_partitions(config, handle, amiga_part, table,
                                             progress)
                    #  A drive imported onto a card built here carries a saved
                    #  screen mode exactly as a whole prepared image does, and
                    #  this ran only for those - so a card whose DH0 came from
                    #  an .hdf opened Workbench on a screen it has not got.
                    if config.patch_display \
                            and config.brings_a_system_from_elsewhere():
                        progress.step("Adapting the display setup on the card")
                        postwrite.adapt_display(handle, amiga_part.start_bytes,
                                                table, config.rtg_display,
                                                progress)
            else:
                parts = mbr.read_table(handle)
                boot_part = _find_boot_partition(parts)
                progress.log(f"Boot partition: {boot_part}")
                fs = Fat32(handle, boot_part.start_bytes)
                _populate_boot(fs, config, emu68_files, emu68_root, progress)
                amiga_part = _find_amiga_partition(parts)
                if amiga_part is not None:
                    check_and_repair(handle, amiga_part.start_bytes,
                                     amiga_part.size_bytes, config, progress)

            if config.expand_to_fill:
                _expand(handle, config, target_size, progress)

            handle.flush()
            os.fsync(handle.fileno())

        progress.step("Flushing to disk")
        if config.target_is_device:
            subprocess.run(["sync"], check=False)
            devices.reread_partition_table(config.target, progress.log)
        progress.fraction(1.0)
        progress.step("Done")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
