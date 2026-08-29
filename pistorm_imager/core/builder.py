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
import enum
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import (amigafs, amigaos, bootcfg, compat, devices, emu68, hdfcheck,
               imgsrc, kickstart, mbr, rdb)
from .fat32 import Fat32
from .util import (MIB, Cancelled, Progress, align_up, copy_stream, human_size,
                   require_tool, run)

SECTOR = 512
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

    #  Post-processing
    expand_to_fill: bool = False
    #  Partitions to create in whatever space an imported drive leaves unused.
    #  Sizes are honoured in order; the last one with size None takes the rest.
    extra_partitions: list[AmigaPartitionSpec] = dataclasses.field(
        default_factory=lambda: [AmigaPartitionSpec("DH1", None, "PFS3", False, -128)])
    extra_boot_files: list[str] = dataclasses.field(default_factory=list)

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
            if self.boot_size < 64 * MIB:
                problems.append("The boot partition must be at least 64 MiB.")
            if not self.amiga_partitions:
                problems.append("Define at least one Amiga partition.")
            flexible = [p for p in self.amiga_partitions if p.size is None]
            if len(flexible) > 1:
                problems.append(
                    "Only one Amiga partition can be set to 'use remaining space'.")
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
    if available > amigafs.FFS_SAFE_LIMIT:
        progress.log(
            f"WARNING: {partition.drive_name} is {human_size(available)}. FFS on "
            f"AmigaOS 3.1 is unreliable above {human_size(amigafs.FFS_SAFE_LIMIT)}; "
            f"a smaller system partition is safer."
        )

    offset = partition.byte_offset(table.geometry, amiga.start_bytes)
    dostype = partition.dostype
    if not amigafs.is_ffs(dostype) and dostype not in (rdb.DOSTYPE_PFS3,
                                                       rdb.DOSTYPE_PDS3):
        raise RuntimeError(
            f"{partition.drive_name} is {rdb.dostype_name(dostype)}; AmigaOS can "
            f"only be installed onto an FFS or PFS3 partition."
        )
    volume = amigaos.install(handle, offset, partition.blocks(table.geometry),
                             chosen, progress,
                             volume_name=config.amiga_volume_name,
                             dostype=dostype, close=False)
    #  Overlays (WHDLoad and the like) go on while the volume is still open;
    #  reopening a finished volume would mean rebuilding its allocation state.
    spec = next((s for s in config.amiga_partitions
                 if s.name.upper() == partition.drive_name.upper()), None)
    if spec is not None and spec.overlays:
        _apply_overlays(volume, spec, _make_fixer(config, progress), progress)
    progress.step("Finalising the Amiga file system")
    volume.close()


def _apply_overlays(volume, spec: AmigaPartitionSpec, fixer,
                    progress: Progress) -> None:
    """Copy extra files or folders on top of a filled partition."""
    for source_text, destination in spec.overlays:
        source = Path(source_text)
        if not source.exists():
            progress.log(f"  overlay missing, skipped: {source}")
            continue
        if source.is_dir():
            copied, _renamed = amigaos.install_tree(volume, source, destination,
                                                    progress, compat=fixer)
            progress.log(f"  overlay: {source.name}/ -> {destination or ':'} "
                         f"({copied} files)")
        else:
            parent = volume.makedirs(destination) if destination else volume.root
            volume.write_file(parent, source.name, source.read_bytes(),
                              check_existing=True)
            progress.log(f"  overlay: {source.name} -> {destination or ':'}")


def _make_fixer(config: BuildConfig, progress: Progress) -> "compat.Compatibility":
    """The compatibility pass, set up the same way wherever it is used."""
    fixer = compat.Compatibility(progress, enabled=config.fix_compatibility,
                                 rtg=config.rtg_display,
                                 native=config.native_display,
                                 workbench_on_rtg=config.workbench_on_rtg)
    if config.spare_files_folder:
        found = fixer.add_spares(config.spare_files_folder)
        if found:
            progress.log(f"{found} replacement file(s) available from "
                         f"{config.spare_files_folder}")
    return fixer


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

        if spec.content_hdf:
            reader, label = amigaos.open_amiga_volume(spec.content_hdf,
                                                      spec.content_hdf_partition)
            progress.step(f"Filling {partition.drive_name} from {label}")
            volume = amigaos.make_volume(handle, offset,
                                         partition.blocks(table.geometry),
                                         spec.volume_name or partition.drive_name,
                                         partition.dostype)
            copied, skipped = amigaos.copy_volume(
                reader, volume, "", progress, skip_existing=False,
                compat=fixer, exclude=spec.exclude)
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
            copied, renamed = amigaos.install_tree(volume, source, "", progress,
                                                   compat=fixer,
                                                   exclude=spec.exclude)
            progress.log(f"{copied} files copied"
                         + (f", {renamed} renamed for AmigaDOS" if renamed else ""))
        else:
            #  Overlay-only partitions are handled where they were installed.
            continue
        _apply_overlays(volume, spec, fixer, progress)
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
    if config.install_amigaos:
        _install_amigaos(config, handle, amiga, table, progress)
    if any(p.content_folder or p.content_hdf
                           for p in config.amiga_partitions):
        _install_content(config, handle, amiga, table, progress)
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
                    if config.install_amigaos:
                        _install_amigaos(config, handle, amiga_part, table, progress)
                    if any(p.content_folder or p.content_hdf
                           for p in config.amiga_partitions):
                        _install_content(config, handle, amiga_part, table, progress)
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
