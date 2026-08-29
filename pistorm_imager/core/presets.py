"""Quick setup: a sensible card built from whatever material is to hand.

The layout chosen here is the one that suits a PiStorm card rather than the one
that suits a 1994 hard drive:

* a **small FFS system partition**, because Kickstart provides FFS with no
  driver, so DH0 mounts on a bare machine no matter what else goes wrong - but
  FFS is slow and needs a full validation pass after an unclean shutdown, so it
  is kept to about a gigabyte;
* a **PFS3 work partition** taking the rest of the card, because SD cards are
  large and FFS on tens of gigabytes is a bad idea.

PFS3 is not in ROM, so its handler has to be embedded in the RDB; the tool can
lift one out of any image that already has it.  We cannot *format* PFS3, so the
work partition is created and formatted on the Amiga.
"""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from . import amigaos, builder, emu68, kickstart, machines, packages
from .util import GIB, MIB, human_size

DEFAULT_BOOT_SIZE = 256 * MIB
DEFAULT_SYSTEM_SIZE = 1 * GIB
MIN_SYSTEM_SIZE = 64 * MIB


@dataclasses.dataclass
class Detected:
    """What we could find without asking the user anything."""

    kickstart: kickstart.RomInfo | None = None
    kickstart_folder: str = ""
    adf_folder: str = ""
    adf_version: str = ""
    adf_complete: bool = False
    adf_summary: str = ""
    pfs3_donor: str = ""
    #  A folder of known-good files used to stand in for anything that cannot
    #  be read from a source drive.
    spare_folder: str = ""
    #  Where the PFS3 handler came from, for the interface to explain itself.
    pfs3_source: str = ""

    @property
    def can_install_os(self) -> bool:
        return bool(self.adf_folder) and self.adf_complete


def _search_roots(extra: list[str] | None = None) -> list[Path]:
    here = Path(__file__).resolve().parent.parent.parent
    #  Deliberately not ~/Downloads: scanning it means opening every .adf in a
    #  potentially enormous folder before the window is even usable.
    roots = [here / "samples", Path.home() / "Amiga"]
    roots += [Path(p) for p in (extra or [])]
    return [r for r in roots if r.is_dir()]


#  Names the PFS3 handler goes by.  The "aio" build is the all-in-one one that
#  serves both the PFS3 and PDS3 DosTypes.
PFS3_HANDLER_NAMES = ["pfs3aio", "pfs3", "pfs3aio020-60", "pfs3ds"]
HUNK_HEADER = b"\x00\x00\x03\xf3"


def _read_handler(path: Path, attempts: int = 3) -> bytes | None:
    """Read a candidate handler, retrying transient errors.

    Sources are often loop-mounted disk images, which do throw the occasional
    ``Errno 5``.  Treating that as "no handler here" once cost a silent failure
    to find a perfectly good file.
    """
    for attempt in range(attempts):
        try:
            data = path.read_bytes()
            return data if data[:4] == HUNK_HEADER else None
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))
    return None


def cached_pfs3_handler() -> Path | None:
    cache = emu68.cache_dir() / "pfs3aio"
    return cache if cache.is_file() and cache.stat().st_size > 0 else None


def find_pfs3_handler(extra_folders: list[str] | None = None) -> tuple[Path, str] | None:
    """Locate a PFS3 handler, returning (path, where it came from).

    PFS3 partitions can now be *created* here, but AmigaOS still cannot *mount*
    one without the handler: it is not in Kickstart, so a copy has to be
    embedded in the RDB.  Rather than asking every time, look for one and keep
    it: an emulator installation usually has ``L:pfs3aio``, and any image with
    PFS3 partitions carries the handler inside its own RDB.
    """
    cached = cached_pfs3_handler()
    if cached is not None:
        return cached, "cached from an earlier build"

    roots = _search_roots(extra_folders)
    #  A plain handler file, which is what an emulator's L: drawer holds - and
    #  what this project keeps in samples/ so a build never has to go looking.
    for root in roots:
        for name in PFS3_HANDLER_NAMES:
            for candidate in list(root.rglob(name))[:4]:
                if not candidate.is_file():
                    continue
                data = _read_handler(candidate)
                if data is not None:
                    return _cache_handler(data), str(candidate)

    #  Otherwise lift one out of the RDB of any hard disk image lying about.
    from . import builder as builder_module
    for root in roots:
        for pattern in ("*.hdf", "*.img"):
            for candidate in list(root.rglob(pattern))[:6]:
                try:
                    with open(candidate, "rb") as handle:
                        located = builder_module.find_rdb(handle)
                except OSError:
                    continue
                if located is None:
                    continue
                for filesystem in located[1].filesystems:
                    if filesystem.seglist:
                        return (_cache_handler(filesystem.seglist),
                                f"{candidate.name} (from its RDB)")
    return None


def _cache_handler(data: bytes) -> Path:
    cache = emu68.cache_dir() / "pfs3aio"
    cache.write_bytes(data)
    return cache


def detect(extra_folders: list[str] | None = None) -> Detected:
    """Look in the obvious places for a Kickstart, Workbench disks and a donor."""
    found = Detected()
    for root in _search_roots(extra_folders):
        if not found.spare_folder:
            found.spare_folder = str(root)
        if found.kickstart is None:
            roms = [r for r in kickstart.scan(root) if r.usable]
            best = best_rom(roms)
            if best is not None:
                found.kickstart = best
                found.kickstart_folder = str(best.path.parent)
        if not found.adf_folder:
            disks = amigaos.scan(root)
            if disks:
                chosen = amigaos.choose_set(disks)
                missing = amigaos.missing_roles(chosen)
                #  Record the folder that actually holds the disks.
                folder = next(iter(chosen.values())).path.parent
                found.adf_folder = str(folder)
                found.adf_version = next(iter(chosen.values())).version
                found.adf_complete = not missing
                names = ", ".join(sorted(m.role.label for m in chosen.values()))
                found.adf_summary = (
                    f"{names}" if not missing
                    else f"{names} (missing "
                         f"{', '.join(r.label for r in missing)})")
    handler = find_pfs3_handler(extra_folders)
    if handler is not None:
        found.pfs3_donor, found.pfs3_source = str(handler[0]), handler[1]
    return found


def best_rom(roms: list[kickstart.RomInfo],
             os_version: str = "") -> kickstart.RomInfo | None:
    """Pick the Kickstart that best suits an Emu68 card.

    Emu68 wants an A1200/AGA ROM.  Where the AmigaOS release being installed is
    known, a matching Kickstart is preferred - Workbench 3.1 belongs with
    Kickstart 3.1, not with 3.2.
    """
    wanted = {"3.1": (40, 68), "3.0": (39, 106), "3.2": (47, 96)}.get(os_version)

    def score(rom: kickstart.RomInfo) -> tuple:
        return (
            1 if rom.usable else 0,
            1 if rom.aga else 0,
            2 if wanted and (rom.version, rom.revision) == wanted else 0,
            1 if (rom.version, rom.revision) in kickstart.KNOWN_ROMS else 0,
            #  Prefer the plain A1200 build over A4000/A3000 dumps of the same
            #  revision, and a 3.1 ROM over an older one.
            1 if "a1200" in rom.path.name.lower().replace(" ", "") else 0,
            rom.version or 0, rom.revision or 0,
        )

    usable = [r for r in roms if r.usable]
    return max(usable, key=score) if usable else None


def quick_config(target: str, target_is_device: bool, card_size: int,
                 detected: Detected, *, board: str = "pistorm32lite",
                 system_size: int = DEFAULT_SYSTEM_SIZE,
                 boot_size: int = DEFAULT_BOOT_SIZE,
                 install_os: bool = True,
                 work_partition: bool = True) -> builder.BuildConfig:
    """Turn the detected material into a ready-to-run build."""
    #  On a small card the default boot partition would swallow everything, so
    #  shrink it rather than refusing; 64 MiB still holds Emu68 and a Kickstart.
    if card_size < boot_size * 3:
        boot_size = max(64 * MIB, min(boot_size, card_size // 4))
    usable = card_size - boot_size - 8 * MIB
    if usable <= MIN_SYSTEM_SIZE:
        raise ValueError(
            f"A {human_size(card_size)} card is too small for a PiStorm setup: "
            f"it needs room for a {human_size(boot_size)} boot partition and an "
            f"Amiga drive.")
    system_size = max(MIN_SYSTEM_SIZE, min(system_size, usable))

    partitions = []
    if work_partition and usable - system_size > 64 * MIB:
        partitions.append(builder.AmigaPartitionSpec(
            "DH0", system_size, "FFS-INTL", bootable=True, boot_priority=0))
        partitions.append(builder.AmigaPartitionSpec(
            "DH1", None, "PFS3", bootable=False, boot_priority=-128))
    else:
        #  Too small to be worth splitting: one FFS system drive.
        partitions.append(builder.AmigaPartitionSpec(
            "DH0", None, "FFS-INTL", bootable=True, boot_priority=0))

    wants_os = install_os and detected.can_install_os
    return builder.BuildConfig(
        mode=builder.BuildMode.FRESH,
        target=target,
        target_is_device=target_is_device,
        image_size=card_size,
        variant=board,
        boot_size=boot_size,
        amiga_partitions=partitions,
        kickstart_path=str(detected.kickstart.path) if detected.kickstart else "",
        pfs3_binary=detected.pfs3_donor,
        install_amigaos=wants_os,
        spare_files_folder=detected.spare_folder,
        adf_folder=detected.adf_folder if wants_os else "",
        adf_version=detected.adf_version if wants_os else "",
        amiga_volume_name="Workbench",
    )


def describe(config: builder.BuildConfig, detected: Detected) -> str:
    """A plain account of what the build will actually put on the card."""
    lines = [f"Boot partition: {human_size(config.boot_size)} FAT32 with Emu68"]
    if config.kickstart_path:
        name = detected.kickstart.name if detected.kickstart else "Kickstart"
        lines.append(f"Kickstart: {name}")
    else:
        lines.append("Kickstart: none found - Emu68 will not start without one")

    filled_system = False
    for spec in config.amiga_partitions:
        size = "the rest of the card" if spec.size is None else human_size(spec.size)
        label = spec.volume_name or spec.name
        shown = spec.name if label == spec.name else f"{spec.name} ({label}:)"
        #  Say what will be *in* it, rather than assuming it needs formatting.
        if spec.content_folder:
            content = f"copied from {Path(spec.content_folder).name}"
            filled_system |= spec.bootable
        elif spec.content_hdf:
            where = (f" partition {spec.content_hdf_partition}"
                     if spec.content_hdf_partition else "")
            content = f"copied from {Path(spec.content_hdf).name}{where}"
            filled_system |= spec.bootable
        elif spec.bootable and config.install_amigaos:
            content = (f"AmigaOS {config.adf_version} installed from your "
                       f"floppy images")
            filled_system = True
        else:
            content = "left empty - format it on the Amiga"
        lines.append(f"{shown}: {size}, {spec.dostype} - {content}")

    if not filled_system:
        lines.append("Nothing will be installed onto the boot drive - partition "
                     "and format it with HDToolBox on the Amiga")
    if config.pfs3_binary:
        origin = detected.pfs3_source or Path(config.pfs3_binary).name
        lines.append(f"PFS3 handler: found ({origin})")
    elif any(s.dostype.startswith(("PFS", "PDS")) for s in config.amiga_partitions):
        lines.append("PFS3 handler: NOT FOUND - the PFS3 partitions will not "
                     "mount until one is supplied, or added from HDToolBox")
    return "\n".join(lines)


# ------------------------------------------------------- machine driven setup

#  PiMiga keeps its WHDLoad collections split by chipset, so a machine that
#  cannot run a category simply does not get it copied.
AGA_ONLY_CATEGORIES = ["WHDLOAD/AGA", "WHDLOAD/CD32"]

#  PiMiga's System drive is around 9 GB, so give it headroom.
PIMIGA_SYSTEM_SIZE = 11 * GIB
#  Below this there is no point taking a 9 GB system and no room for games.
PIMIGA_SYSTEM_MIN_CARD = 24 * GIB


def choose_system_source(display: machines.Display, disks: "Path | None",
                         card_size: int, requested: str = "auto") -> str:
    """Decide where the operating system comes from: "pimiga" or "adf".

    A ready-made system saves installing one, but PiMiga's is built around
    Picasso96 and ships saved screen-mode preferences, so on a machine being
    watched on its own video output it can boot to a screen nobody can see.
    That, and its size, is why the default is not simply "use what is there".
    """
    have = disks is not None and (disks / "System").is_dir()
    if requested == "none":
        return "none"
    if requested == "pimiga":
        return "pimiga" if have else "adf"
    if requested == "adf":
        return "adf"
    #  Choosing a PiMiga source means using it: take its system and adapt the
    #  graphics for the target, rather than quietly installing a different OS.
    return "pimiga" if have else "adf"


#  Where PiMiga keeps each drive, relative to its "disks" folder.
#  PiMiga's own assignment, from its Amiberry configuration: DH0:System,
#  DH1:Demos, DH2:Games, DH3:Work.  Copying "the PiMiga setup" means keeping
#  those device names and volume labels, not inventing new ones.
PIMIGA_DRIVES = [
    ("DH1", "Demos", 0.20),
    ("DH2", "Games", 0.55),
    ("DH3", "Work", 0.25),
]


def pimiga_disks(folder: str | Path) -> Path | None:
    """Find PiMiga's ``disks`` directory, given it or its parent."""
    folder = Path(folder)
    for candidate in (folder, folder / "disks", folder / "pimiga" / "disks"):
        if (candidate / "System").is_dir() and (candidate / "Games").is_dir():
            return candidate
    return None


def excluded_for(machine: machines.Machine) -> list[str]:
    """Content categories this machine cannot use."""
    return [] if machine.aga else list(AGA_ONLY_CATEGORIES)


def package_overlays(donor: str | Path | None, keys: list[str] | None,
                     rtg: bool) -> list[tuple[str, str]]:
    """Optional software to lay on top of a Workbench built from floppies.

    A Workbench installed from the original disks has no archiver, no installer
    and no WHDLoad, so the pieces almost everyone adds next are offered here.
    They are copied out of a system the user already has rather than shipped.
    """
    if donor is None:
        return []
    chosen = packages.default_keys(rtg) if keys is None else keys
    return packages.overlays_for(donor, chosen, rtg)


def machine_setup(machine: machines.Machine, display: machines.Display,
                  target: str, target_is_device: bool, card_size: int,
                  detected: Detected, *, pimiga_folder: str = "",
                  hdmi: tuple[int | None, int | None] = (None, None),
                  system_size: int = DEFAULT_SYSTEM_SIZE,
                  boot_size: int = DEFAULT_BOOT_SIZE,
                  trapdoor_to_chip: bool = False,
                  system_source: str = "auto",
                  hdf_source: str = "",
                  work_partition: bool = True,
                  package_donor: str = "",
                  package_keys: list[str] | None = None) -> builder.BuildConfig:
    """A complete card for one machine.

    The content is the same on every model - Workbench, WHDLoad, the games and
    demos.  What the machine decides is the Emu68 build, the Kickstart, the
    display settings, and which chipset-specific content is worth copying.
    """
    disks = pimiga_disks(pimiga_folder) if pimiga_folder else None
    if hdf_source and system_source in ("auto", "image"):
        source = "image"
    elif system_source == "image" and not hdf_source:
        source = choose_system_source(display, disks, card_size, "auto")
    else:
        source = choose_system_source(display, disks, card_size, system_source)
    config = quick_config(target, target_is_device, card_size, detected,
                          board=machine.board, system_size=system_size,
                          boot_size=boot_size,
                          install_os=source == "adf",
                          #  A source brings its own drives; otherwise the
                          #  caller decides whether a work drive is wanted.
                          work_partition=work_partition and not pimiga_folder)
    config.boot_options = machines.boot_options(machine, display, hdmi,
                                                trapdoor_to_chip)
    config.rtg_display = display.uses_rtg

    #  Prefer a Kickstart that suits this machine.
    if detected.kickstart is not None:
        roms = [detected.kickstart]
        best = best_rom_for_machine(roms, machine)
        if best is not None:
            config.kickstart_path = str(best.path)

    if not config.pfs3_binary and disks is not None:
        handler = find_pfs3_handler([str(disks / "System" / "L")])
        if handler is not None:
            config.pfs3_binary = str(handler[0])
    system = config.amiga_partitions[0]
    config.system_source = source
    if source == "image":
        #  Mirror the source image's own partition scheme onto the card.
        config.install_amigaos = False
        config.adf_folder = ""
        available = card_size - boot_size - 16 * MIB
        config.amiga_partitions = partitions_from_image(hdf_source, machine,
                                                        available)
        return config
    if source == "pimiga":
        #  Take the ready-made system instead of installing one from floppies.
        system.content_folder = str(disks / "System")
        system.volume_name = "System"
        system.dostype = "PFS3"
        system.overlays = []
        config.install_amigaos = False
        config.adf_folder = ""
        system_size = max(system_size, PIMIGA_SYSTEM_SIZE)
    elif source == "none":
        #  Leave the drive to be partitioned and formatted on the Amiga.
        config.install_amigaos = False
        config.adf_folder = ""
        system.overlays = []
    else:
        #  A Workbench installed from floppies has never heard of WHDLoad, an
        #  archiver, or the installer most software expects.
        system.overlays = package_overlays(
            package_donor or pimiga_folder or None, package_keys,
            display.uses_rtg)

    if disks is not None:
        #  The system partition must take a fixed size now: the PiMiga drives
        #  that follow need somewhere to fit, and only one partition can be
        #  left to soak up whatever is spare.
        system.size = min(system_size, max(MIN_SYSTEM_SIZE,
                                           card_size - boot_size - 64 * MIB))
        exclude = excluded_for(machine)
        remaining = card_size - boot_size - system.size - 16 * MIB
        if remaining <= 0:
            raise ValueError(
                f"A {human_size(card_size)} card leaves no room for the games "
                f"and demos after a {human_size(system.size)} system drive.")
        #  Games get the lion's share, then demos, then work.
        extra: list[builder.AmigaPartitionSpec] = []
        present = [(name, drive, share) for name, drive, share in PIMIGA_DRIVES
                   if (disks / drive).is_dir()]
        for index, (name, drive, share) in enumerate(present):
            last = index == len(present) - 1
            extra.append(builder.AmigaPartitionSpec(
                name=name, volume_name=drive,
                size=None if last else int(remaining * share),
                dostype="PFS3", bootable=False, boot_priority=-128,
                content_folder=str(disks / drive),
                exclude=exclude if drive in ("Games", "Demos") else []))
        config.amiga_partitions = [system] + extra
    return config


def best_rom_for_machine(roms: list[kickstart.RomInfo],
                         machine: machines.Machine) -> kickstart.RomInfo | None:
    """Pick the Kickstart this machine would prefer, from those available."""
    wanted = machine.kickstarts

    def score(rom: kickstart.RomInfo) -> tuple:
        pair = (rom.version or 0, rom.revision or 0)
        rank = len(wanted) - wanted.index(pair) if pair in wanted else 0
        return (1 if rom.usable else 0, rank, 1 if rom.aga else 0)

    usable = [r for r in roms if r.usable]
    return max(usable, key=score) if usable else None


SYSTEM_SOURCE_LABELS = {
    "image": "an Amiga hard disk image, with its own partition scheme",
    "pimiga": "PiMiga's ready-made system (AmigaOS 3.9, Scalos)",
    "adf": "Workbench installed from your floppy images",
    "none": "none - the drive is left for HDToolBox on the Amiga",
}


def describe_machine_setup(config: builder.BuildConfig,
                           machine: machines.Machine,
                           display: machines.Display,
                           detected: Detected) -> str:
    source = getattr(config, "system_source", "adf")
    lines = [f"{machine.label} with {machine.board_label}",
             f"Display: {display.label}",
             f"System: {SYSTEM_SOURCE_LABELS.get(source, source)}"]
    lines.append("")
    lines.append(describe(config, detected))
    cmdline = config.boot_options.cmdline()
    if cmdline:
        lines.append(f"Emu68 options: {cmdline}")
    lines.append("")
    for note in machines.advice(machine, display):
        lines.append(f"- {note}")
    return "\n".join(lines)


def partitions_from_image(path: str | Path, machine: machines.Machine,
                          available: int) -> list[builder.AmigaPartitionSpec]:
    """Mirror the partition scheme of a source image.

    Whatever an imported drive was laid out as - one system partition, or a
    system plus games and work - is what the user expects to see on the card,
    so the scheme is taken from the source rather than imposed. Sizes are
    scaled to whatever space is actually available.
    """
    from . import amigaos, builder as builder_module, rdb

    path = Path(path)
    with open(path, "rb") as handle:
        located = builder_module.find_rdb(handle)

    if located is None:
        #  A bare file system: one partition holding the lot.
        return [builder.AmigaPartitionSpec(
            "DH0", None, "PFS3", bootable=True, boot_priority=0,
            content_hdf=str(path))]

    table = located[1]
    sizes = [p.size_bytes(table.geometry) for p in table.partitions]
    total = sum(sizes) or 1
    exclude = excluded_for(machine)

    specs: list[builder.AmigaPartitionSpec] = []
    for index, partition in enumerate(table.partitions):
        last = index == len(table.partitions) - 1
        share = int(available * sizes[index] / total)
        name = rdb.dostype_name(partition.dostype)
        dostype = "PFS3" if name in ("PFS3", "PDS3") else (
            "FFS-INTL" if name.startswith("FFS") or name.startswith("OFS")
            else "PFS3")
        specs.append(builder.AmigaPartitionSpec(
            name=partition.drive_name,
            size=None if last else max(64 * MIB, share),
            dostype=dostype,
            bootable=partition.bootable,
            boot_priority=partition.boot_priority,
            content_hdf=str(path),
            content_hdf_partition=partition.drive_name,
            exclude=exclude if not partition.bootable else []))
    return specs


def describe_image_scheme(path: str | Path) -> str:
    """A one-line summary of what a source image contains."""
    from . import builder as builder_module
    try:
        with open(path, "rb") as handle:
            located = builder_module.find_rdb(handle)
    except OSError as error:
        return f"cannot read: {error}"
    if located is None:
        return "a single bare file system, with no partition table"
    from . import rdb
    table = located[1]
    parts = ", ".join(
        f"{p.drive_name} ({rdb.dostype_name(p.dostype)}"
        f"{', bootable' if p.bootable else ''})" for p in table.partitions)
    return f"{len(table.partitions)} partition(s): {parts}"


# ------------------------------------------------- what is inside an image

#  Markers that say what a drive can actually do.  A precise AmigaOS version is
#  not reliably recorded anywhere on disk - the obvious candidates give the
#  version of the *command*, not the system - so report capabilities, which is
#  what the choice actually turns on.
SYSTEM_MARKERS = [
    ("bootable", "S/Startup-Sequence", "boots on its own"),
    ("workbench", "C/LoadWB", "has Workbench"),
    ("rtg", "Libs/Picasso96", "has Picasso96 RTG"),
    ("whdload", "C/WHDLoad", "has WHDLoad"),
    ("scalos", "C/Scalos", "uses Scalos"),
]


@dataclasses.dataclass
class ImageSystem:
    """What an imported drive brings with it."""

    label: str = ""
    found: dict = dataclasses.field(default_factory=dict)
    error: str = ""

    @property
    def bootable(self) -> bool:
        return bool(self.found.get("bootable"))

    @property
    def needs_floppies(self) -> bool:
        """True when the image cannot supply an operating system itself."""
        return not self.bootable

    def describe(self) -> str:
        if self.error:
            return f"could not be examined: {self.error}"
        if not self.bootable:
            return ("no operating system on this drive - install one from "
                    "floppy images, or it will not boot")
        traits = [text for key, _path, text in SYSTEM_MARKERS
                  if key != "bootable" and self.found.get(key)]
        detail = f"; {', '.join(traits)}" if traits else ""
        return f"a complete system that {SYSTEM_MARKERS[0][2]}{detail}"


def installed_monitors(reader) -> list[str]:
    """Monitor drivers a system has installed, from DEVS:Monitors.

    STORAGE:Monitors is deliberately not consulted: AmigaOS ships the whole set
    there uninstalled, so its contents say nothing about what a system expects.
    """
    entry = reader.find("Devs/Monitors")
    if entry is None:
        return []
    handle = getattr(entry, "block", None)
    if handle is None:
        handle = getattr(entry, "anode", None)
    if handle is None:
        return []
    return [e.name for e in reader.listdir(handle)
            if not e.name.lower().endswith(".info")]


def check_image_for_machine(path: str | Path, machine: machines.Machine,
                            partition: str = "") -> list[str]:
    """Warn where an imported system expects more hardware than the target has.

    Plenty of ready-made drives are built for an A1200 and say so only by the
    display modes they install; put one on an A500 and Workbench opens in a mode
    the chipset cannot produce.
    """
    from . import amigaos

    warnings: list[str] = []
    try:
        reader, _label = amigaos.open_amiga_volume(path, partition)
    except Exception as error:  # noqa: BLE001 - reported, never raised at the user
        return [f"could not be examined: {error}"]
    try:
        monitors = installed_monitors(reader)
        beyond = machines.monitors_beyond(machine, monitors)
        if beyond:
            names = ", ".join(f"{n} (needs {c.value})" for n, c in beyond)
            warnings.append(
                f"installs display modes this machine cannot produce: {names}. "
                f"Workbench may open on a screen the {machine.chipset.value} "
                f"chipset cannot show.")
        if reader.find("Prefs/Env-Archive/Sys/ScreenMode.prefs") is not None \
                and beyond:
            warnings.append(
                "it also carries a saved screen mode, which is what Workbench "
                "will try to use; it is removed automatically when the display "
                "is the Amiga's own video output.")
    finally:
        try:
            reader.f.close()
        except Exception:  # noqa: BLE001
            pass
    return warnings


def inspect_image_system(path: str | Path, partition: str = "") -> ImageSystem:
    """Look inside an image to see whether it brings its own operating system."""
    from . import amigaos

    try:
        reader, label = amigaos.open_amiga_volume(path, partition)
    except Exception as error:  # noqa: BLE001 - report, never raise at the user
        return ImageSystem(error=str(error))
    try:
        found = {}
        for key, location, _text in SYSTEM_MARKERS:
            try:
                found[key] = reader.find(location) is not None
            except Exception:  # noqa: BLE001 - a damaged tree is not fatal here
                found[key] = False
        return ImageSystem(label=label, found=found)
    finally:
        try:
            reader.f.close()
        except Exception:  # noqa: BLE001
            pass
