"""BoingBags: the official and community updates to AmigaOS 3.5 and 3.9.

A CD install is not a finished system.  Both releases had post-release update
packs - "BoingBags" - and 3.9 without BoingBag 2 is missing a great deal.  They
are LhA archives rather than part of the disc, so they are a separate layer
applied on top of whatever the CD laid down, in the order they were published.

**They are not all the same shape**, and the difference decides what this
project can do on Linux:

* **BoingBag 3.5-1 and 3.5-2** are plain trees.  ``Workbench`` goes to the root
  of the drive, ``Printers`` to ``Devs/Printers``, and so on - a copy, and this
  project can do all of it.
* **BoingBag 3.9-3&4** is a community release and also plain, but its
  ``Files2`` drawer is not a blanket copy: it holds several builds of the same
  file, one per processor and one per machine, and the installer picks.  That
  is the same rule this project already applies to packages - keep the binary
  this machine runs - and it is applied here from the same evidence, the
  ``newname`` clauses in the pack's own Installer script.
* **BoingBag 3.9-1 and 3.9-2** keep their system fixes in ``AmigaOS-Update``, a
  ZIP in which every one of the 233 entries is encrypted.  The password lives
  inside Haage & Partner's ``Updater`` binary, and their installer simply runs
  ``C/Updater AmigaOS-Update <target>``.

For that last case the answer is to run their tool rather than to break their
archive open: the build boots the drive it has just made in FS-UAE and lets
``Updater`` do the work it was written for.  When FS-UAE is not installed the
files that *are* in the clear are still applied, and the build says plainly
which fixes were left out - a card that is quietly half-patched would be worse
than one that is honestly incomplete.

Every source and destination below was read out of each pack's own Installer
script, for the same reason as the CD layout in ``amigacd.py``: several are not
the obvious ones, and a wrong destination makes a system that looks installed.
"""
from __future__ import annotations

import dataclasses
import zipfile
from pathlib import Path

from . import machines
from .machines import Cpu
from .util import Progress

#  The tree inside the drive that an update lands on, when it is a plain copy.
@dataclasses.dataclass(frozen=True)
class Layer:
    source: str                 # relative to the pack's own root drawer
    destination: str            # "" is the root of the drive
    label: str
    order: int


@dataclasses.dataclass(frozen=True)
class Variant:
    """One file chosen from several builds of it.

    ``candidates`` is tried in order and the first whose condition holds wins,
    so the last entry is the fallback.  Each is (condition, source), where the
    condition is a callable taking the processor and whether an FPU is present.
    """

    candidates: tuple[tuple[str, str], ...]     # (rule, source path)
    destination: str
    newname: str
    label: str


@dataclasses.dataclass(frozen=True)
class Bag:
    key: str
    label: str
    release: str                # "3.5" or "3.9"
    order: int                  # the order they were published in
    root: str                   # the drawer the archive unpacks into
    layers: tuple[Layer, ...] = ()
    variants: tuple[Variant, ...] = ()
    #  Payloads only the publisher's own tool can open.  There can be more
    #  than one: BoingBag 3.9-2 locks XAD-Update alongside AmigaOS-Update.
    locked_payloads: tuple[str, ...] = ()
    official: bool = True
    #  Whether this pack is offered by default.  BoingBag 3&4 changes core
    #  components - dos.library, icon.library, the TCP/IP stack - so it is
    #  separable even though it is on by default: somebody chasing a fault has
    #  to be able to take it back off and get a stock system.
    default_on: bool = True
    notes: str = ""


#  ------------------------------------------------------------------ 3.5
#
#  Both 3.5 packs share a layout; only the Internet drawer moved between them.
def _os35_layers(internet_destination: str) -> tuple[Layer, ...]:
    return (
        Layer("Workbench", "", "Workbench updates", 10),
        Layer("Printers", "Devs/Printers", "Printer drivers", 20),
        Layer("ROM-Update", "Devs", "ROM update", 30),
        Layer("Internet", internet_destination, "Internet software", 40),
    )


#  ------------------------------------------------------------------ 3.9
#
#  Files1 is copied wholesale to the root of the drive; Files2 is picked over.
BB34_LAYERS = (
    Layer("Files1", "", "System updates", 10),
    Layer("Files2/Devs/Keymaps", "Devs/Keymaps", "Keymaps", 20),
    Layer("Files2/Fonts", "Fonts", "Fonts", 25),
    Layer("Files2/Locale/Countries", "Locale/Countries", "Countries", 30),
    Layer("Files2/Locale/Catalogs", "Locale/Catalogs", "Catalogs", 35),
    Layer("Files2/Libs/Picasso96", "Libs/Picasso96", "Picasso96", 40),
)

#  The processor- and machine-specific builds.  The rules are the ones the
#  pack's own script tests - (database "cpu" "68060"), (database "fpu" "NOFPU")
#  and the machine model - and the names are its (newname) clauses.
BB34_VARIANTS = (
    Variant(
        candidates=(("cpu68060", "Files2/Libs/xadmaster_060.library"),
                    ("always", "Files2/Libs/xadmaster_020.library")),
        destination="Libs", newname="xadmaster.library", label="xadmaster"),
    Variant(
        candidates=(("cpu68060+fpu", "Files2/Libs/mpega060FPU.library"),
                    ("cpu68060", "Files2/Libs/mpega060.library"),
                    ("cpu68040+fpu", "Files2/Libs/mpega040FPU.library"),
                    ("cpu68040", "Files2/Libs/mpega040.library"),
                    ("always", "Files2/Libs/mpega020FPU.library")),
        destination="Libs", newname="mpega.library", label="mpega"),
    #  scsi.device is the machine's own IDE controller, so it is only replaced
    #  on the machines that have one.  An A500 or A2000 has no internal IDE at
    #  all, and putting an A1200's driver on one would be inventing hardware.
    Variant(
        candidates=(("a600", "Files2/Devs/scsi_A600_A1200.device"),
                    ("a600", "Files2/Devs/scsi_A600.device"),
                    ("a1200", "Files2/Devs/scsi_A600_A1200.device"),
                    ("a1200", "Files2/Devs/scsi_A1200.device")),
        destination="Devs", newname="scsi.device", label="IDE driver"),
)

BAGS = (
    Bag("3.5-1", "BoingBag 1 for AmigaOS 3.5", "3.5", 10, "BoingBag_1",
        layers=_os35_layers("")),
    Bag("3.5-2", "BoingBag 2 for AmigaOS 3.5", "3.5", 20, "BoingBag2",
        layers=_os35_layers("Internet")),
    Bag("3.9-1", "BoingBag 1 for AmigaOS 3.9", "3.9", 10, "BoingBag3.9-1",
        layers=(Layer("Locale", "Locale", "Locale", 10),
                Layer("Contribution", "Contribution", "Contributions", 20),
                Layer("Internet", "Internet", "Internet software", 30)),
        locked_payloads=("AmigaOS-Update",),
        notes="The system fixes are in an encrypted archive that only Haage & "
              "Partner's Updater can open."),
    Bag("3.9-2", "BoingBag 2 for AmigaOS 3.9", "3.9", 20, "BoingBag3.9-2",
        locked_payloads=("AmigaOS-Update", "XAD-Update"),
        notes="Carries no plain files at all - C and Installer are the "
              "Updater's own tooling and Manuals is documentation, so every "
              "fix it makes is inside its two encrypted archives."),
    Bag("3.9-34", "BoingBags 3 & 4 for AmigaOS 3.9", "3.9", 30,
        "BoingBag3.9-3&4", layers=BB34_LAYERS, variants=BB34_VARIANTS,
        official=False,
        notes="A community release that supersedes much of BoingBags 1 and 2 "
              "with newer versions - dos.library 42.1, icon.library 46.4, "
              "FastFileSystem 45.16 - and adds LBA48 large-disk support. It "
              "expects BoingBags 1 and 2 underneath it."),
)

BAGS_BY_KEY = {b.key: b for b in BAGS}


def for_release(release: str) -> tuple[Bag, ...]:
    """The packs that belong to one AmigaOS release, oldest first."""
    return tuple(sorted((b for b in BAGS if b.release == release),
                        key=lambda b: b.order))


def _rule_holds(rule: str, machine: machines.Machine,
                cpu: Cpu, has_fpu: bool) -> bool:
    if rule == "always":
        return True
    if rule == "cpu68060":
        return cpu is Cpu.M68060
    if rule == "cpu68060+fpu":
        return cpu is Cpu.M68060 and has_fpu
    if rule == "cpu68040":
        return cpu is Cpu.M68040
    if rule == "cpu68040+fpu":
        return cpu is Cpu.M68040 and has_fpu
    return rule == machine.key


def choose_variant(variant: Variant, machine: machines.Machine,
                   cpu: Cpu, has_fpu: bool,
                   root: Path | None = None) -> str:
    """Which build of a file this machine wants, or "" for none of them.

    A candidate whose rule holds but whose file is not in this copy of the pack
    is passed over rather than chosen and then found missing.  The packs in
    circulation do not all agree with their own scripts - the community release
    ships one ``scsi_A600_A1200.device`` where its Installer still names an
    A600 and an A1200 file separately - so both spellings are listed and
    whichever is actually present wins.
    """
    for rule, source in variant.candidates:
        if not _rule_holds(rule, machine, cpu, has_fpu):
            continue
        if root is not None and not (root / source).is_file():
            continue
        return source
    return ""


def stage(bag: Bag, archive_root: Path, into: str | Path,
          machine: machines.Machine, cpu: Cpu, has_fpu: bool,
          progress: Progress | None = None) -> int:
    """Lay a pack's plain files over a staged system tree.

    Over, not beside: an update replaces what the disc installed, so this runs
    on the Linux staging tree where a later copy wins, for the same reason the
    CD's own layers do.

    A layer that is not in this copy of the pack is skipped rather than
    complained about.  The packs genuinely differ - BoingBag 3.5-2 ships no
    printer drivers where 3.5-1 does - so an absent tree is a fact about the
    release, not a fault.
    """
    into = Path(into)
    written = 0
    for layer in sorted(bag.layers, key=lambda item: item.order):
        source = archive_root / layer.source
        if not source.is_dir():
            continue
        destination = into / layer.destination if layer.destination else into
        count = _copy_over(source, destination)
        written += count
        if progress is not None:
            progress.log(f"  {bag.label}: {layer.label}, {count} files -> "
                         f"{layer.destination or 'the root of the drive'}")

    for variant in bag.variants:
        source = choose_variant(variant, machine, cpu, has_fpu, archive_root)
        if not source:
            continue
        destination = into / variant.destination / variant.newname
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((archive_root / source).read_bytes())
        written += 1
        if progress is not None:
            progress.log(f"  {bag.label}: {variant.label} - "
                         f"{Path(source).name} installed as "
                         f"{variant.destination}/{variant.newname}")
    return written


def _copy_over(source: Path, destination: Path) -> int:
    """Copy a tree, replacing what is already there."""
    written = 0
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.read_bytes())
        written += 1
    return written


def locked_entries(archive_root: Path, bag: Bag) -> list[str]:
    """The file names inside a pack's encrypted payload.

    Read from the ZIP's directory, which is not encrypted even though every
    file in it is.  That is what lets a build say exactly which fixes it could
    not apply instead of only that some exist.
    """
    names: list[str] = []
    for name in bag.locked_payloads:
        payload = archive_root / name
        if not payload.is_file():
            continue
        try:
            with zipfile.ZipFile(payload) as bundle:
                names += [item.filename for item in bundle.infolist()
                          if not item.is_dir()]
        except (zipfile.BadZipFile, OSError):
            continue
    return names


def is_locked(archive_root: Path, bag: Bag) -> bool:
    """Whether the payload really is encrypted, rather than assumed to be."""
    for name in bag.locked_payloads:
        payload = archive_root / name
        if not payload.is_file():
            continue
        try:
            with zipfile.ZipFile(payload) as bundle:
                if any(item.flag_bits & 0x1 for item in bundle.infolist()):
                    return True
        except (zipfile.BadZipFile, OSError):
            continue
    return False


def find_root(unpacked: Path, bag: Bag) -> Path | None:
    """Where a pack's own drawer ended up after unpacking.

    Archives are not consistent about how deep they nest: BoingBag39-1.lha puts
    ``BoingBag3.9-1`` at the top, while BB1-4.lha carries all three packs side
    by side one level down.  So the drawer is searched for by name rather than
    assumed to be at any particular depth.
    """
    if (unpacked / bag.root).is_dir():
        return unpacked / bag.root
    for candidate in unpacked.rglob(bag.root):
        if candidate.is_dir():
            return candidate
    return None


def describe(bag: Bag, archive_root: Path | None) -> str:
    """One line about what this pack will and will not manage to do."""
    if archive_root is None:
        return f"{bag.label}: not found"
    locked = locked_entries(archive_root, bag) if is_locked(archive_root, bag) else []
    if locked:
        return (f"{bag.label}: {len(locked)} system files are in an encrypted "
                f"archive and need its own Updater to apply")
    return f"{bag.label}: ready"


def report_skipped(bag: Bag, archive_root: Path,
                   progress: Progress) -> list[str]:
    """Say, file by file, which fixes were not applied and why.

    Named rather than counted, because "some fixes were skipped" is not
    something anybody can act on, and the list is what tells you whether the
    thing you are chasing is in it.
    """
    names = locked_entries(archive_root, bag)
    if not names:
        return []
    progress.log(f"  {bag.label}: {len(names)} system files could not be "
                 f"applied without FS-UAE, because they are in an encrypted "
                 f"archive only its own Updater can open:")
    for name in sorted(names):
        progress.log(f"    left out: {name}")
    return names
