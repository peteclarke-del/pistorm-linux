"""Installing AmigaOS 3.5 and 3.9 from their CD images.

These two releases were sold on CD rather than floppy, so ``amigaos.py`` - which
recognises install disks by the volume name inside an ADF - has nothing to work
with here.  What a CD offers instead is better: the system is already laid out
as directory trees, so an install is a copy in the right order rather than a
disk-by-disk merge.

**Where the layout comes from.**  Every source and destination below was read
out of the installer script each disc carries - ``OS-Version3.5/OS3.5Install``
and ``OS-Version3.9/OS3.9Install``, which are plain Installer text.  That
matters more than it sounds: several of the destinations are not the obvious
ones, and guessing them would produce a system that looks installed and is
subtly wrong.  ``Extras/Backdrops`` goes to ``Prefs/Presets/Backdrops`` and not
to ``Backdrops``; the printer and keymap sets are lifted out of the Workbench
tree's own ``Storage`` and copied *again* into ``Devs``.

**Why the order matters.**  Neither release is self-contained, and the two discs
are not layered the same way:

* The **3.9 disc** carries ``Workbench3.5`` and ``Workbench3.9`` side by side.
  ``Workbench3.5`` there is a complete system; ``Workbench3.9`` is the overlay
  that turns it into 3.9.  Its own installer copies both, in that order, for a
  3.9 install.
* The **3.5 disc** carries a *delta*.  ``OS-Version3.5/Workbench`` has no ``S``,
  no ``WBStartup`` and no ``Rexxc``, because it is meant to land on an existing
  Workbench 3.1 - which the disc also supplies, under ``OS-Version3.1``.

So a release is a list of layers copied newest-last, exactly as the floppy roles
in ``amigaos.py`` are ordered so that Workbench beats Extras.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from . import iso9660, machines
from .machines import Cpu
from .util import Progress

#  AmigaOS 3.5 and 3.9 both refuse to run on anything below a 68020, and both
#  want a Kickstart 3.1 (V40) underneath.  The Kickstart is the requirement that
#  can actually bite on a PiStorm, because Emu68 always provides a 68040-class
#  core but the ROM mapped from the boot partition is whatever was chosen.
NEEDS_CPU = Cpu.M68020
NEEDS_KICKSTART = 40


@dataclasses.dataclass(frozen=True)
class Layer:
    """One tree copied off the disc, and where it lands on the system drive."""

    source: str                 # a path inside the ISO
    destination: str            # "" is the root of the drive
    label: str
    order: int
    required: bool = False


@dataclasses.dataclass(frozen=True)
class Release:
    key: str
    label: str
    #  The volume name the disc carries, used to tell one disc from the other.
    volume: str
    layers: tuple[Layer, ...]

    @property
    def needs_cpu(self) -> Cpu:
        return NEEDS_CPU

    @property
    def needs_kickstart(self) -> int:
        return NEEDS_KICKSTART


#  ---------------------------------------------------------------- 3.5 disc
#
#  "Workbench" here is the 3.5 delta, so the 3.1 trees the disc also carries
#  have to be laid down first or the result has no Startup-Sequence at all.
OS35_LAYERS = (
    Layer("OS-Version3.1/Workbench3.1", "", "Workbench 3.1", 10, required=True),
    Layer("OS-Version3.1/Extras3.1", "", "Extras 3.1", 20),
    Layer("OS-Version3.5/Workbench", "", "Workbench 3.5", 30, required=True),
    Layer("OS-Version3.5/Locale", "Locale", "Locale", 40),
    Layer("OS-Version3.5/Keymaps", "Devs/Keymaps", "Keymaps", 50),
    Layer("OS-Version3.5/Printers", "Devs/Printers", "Printer drivers", 55),
    Layer("OS-Version3.5/C", "C", "Updated commands", 60),
    Layer("OS-Version3.5/L", "L", "FastFileSystem", 65),
    Layer("OS-Version3.5/Extras/Libs", "Libs", "Extra libraries", 70),
    Layer("OS-Version3.5/Extras/Backdrops", "Prefs/Presets/Backdrops",
          "Backdrops", 75),
)

#  ---------------------------------------------------------------- 3.9 disc
#
#  Workbench3.5 is complete on this disc, so no 3.1 layer is needed.  Printers
#  and keymaps come out of the 3.9 tree's own Storage drawer, which is where
#  its installer takes them from.
OS39_LAYERS = (
    Layer("OS-Version3.9/Workbench3.5", "", "Workbench 3.5 base", 10,
          required=True),
    Layer("OS-Version3.9/Workbench3.9", "", "Workbench 3.9", 20, required=True),
    Layer("OS-Version3.9/Locale", "Locale", "Locale", 30),
    Layer("OS-Version3.9/Workbench3.9/Storage/Keymaps", "Devs/Keymaps",
          "Keymaps", 40),
    Layer("OS-Version3.9/Workbench3.9/Storage/Printers", "Devs/Printers",
          "Printer drivers", 45),
    Layer("OS-Version3.9/C", "C", "Updated commands", 50),
    Layer("OS-Version3.9/L", "L", "FastFileSystem", 55),
    Layer("OS-Version3.9/Extras/Libs", "Libs", "Extra libraries", 60),
    Layer("OS-Version3.9/Extras/Backdrops", "Prefs/Presets/Backdrops",
          "Backdrops", 65),
)

RELEASES = (
    Release("3.5", "AmigaOS 3.5", "AmigaOS3.5", OS35_LAYERS),
    Release("3.9", "AmigaOS 3.9", "AmigaOS3.9", OS39_LAYERS),
)

RELEASES_BY_KEY = {r.key: r for r in RELEASES}


@dataclasses.dataclass
class CdMatch:
    """What was found on a disc image handed to us."""

    path: Path
    volume_name: str = ""
    release: Release | None = None
    #  Only the layers actually present, so a disc that is missing a tree says
    #  so rather than failing part-way through a build.
    present: tuple[Layer, ...] = ()
    missing: tuple[Layer, ...] = ()
    files: int = 0
    total_bytes: int = 0
    error: str = ""

    @property
    def usable(self) -> bool:
        return (self.release is not None and not self.error
                and not any(layer.required for layer in self.missing))

    @property
    def label(self) -> str:
        if self.error:
            return f"{self.path.name}: {self.error}"
        if self.release is None:
            return f'{self.path.name}: "{self.volume_name}" is not an AmigaOS CD'
        note = "" if self.usable else " - incomplete"
        return (f'"{self.volume_name}" -> {self.release.label}{note} '
                f"({self.files} files)")


def identify(path: str | Path) -> CdMatch:
    """Work out which AmigaOS release a CD image holds, by looking inside it.

    By the volume name *and* the trees, not by the file name: the discs in
    circulation are named every possible way, and a name is not evidence.
    """
    path = Path(path)
    try:
        with iso9660.IsoImage.open(path) as iso:
            volume = iso.volume_name
            release = next((r for r in RELEASES
                            if r.volume.lower() == volume.strip().lower()), None)
            if release is None:
                #  The volume name is the quick answer; if it is not one we
                #  know, fall back to looking for the trees themselves, because
                #  a re-mastered disc keeps the layout and loses the label.
                release = _release_by_layout(iso)
            if release is None:
                return CdMatch(path, volume_name=volume)

            present, missing = [], []
            files = total = 0
            for layer in release.layers:
                entry = iso.find(layer.source)
                if entry is None or not entry.is_dir:
                    missing.append(layer)
                    continue
                present.append(layer)
                for _relative, child in iso.walk(entry):
                    if not child.is_dir:
                        files += 1
                        total += child.size
            return CdMatch(path, volume_name=volume, release=release,
                           present=tuple(present), missing=tuple(missing),
                           files=files, total_bytes=total)
    except (iso9660.Iso9660Error, OSError, ValueError) as error:
        return CdMatch(path, error=str(error))


def _release_by_layout(iso: iso9660.IsoImage) -> Release | None:
    """Recognise a disc whose volume name has been changed, by its trees."""
    for release in RELEASES:
        required = [layer for layer in release.layers if layer.required]
        if required and all(iso.find(layer.source) is not None
                            for layer in required):
            return release
    return None


def stage(match: CdMatch, into: str | Path,
          progress: Progress | None = None) -> int:
    """Lay the disc's trees out on disk, in order, as the drive should look.

    The layering is resolved here rather than on the Amiga volume, and that is
    not an implementation detail: the volume writer in this project creates
    files and never overwrites them, so whatever lands first wins.  Copying the
    3.5 tree and then the 3.9 tree straight onto it would keep the *3.5* file
    every time the two discs carry the same name - which is every important
    file on the disc, and exactly backwards.

    On a plain Linux directory a later copy replaces an earlier one, so the
    order below produces the tree the Amiga should end up with, and the volume
    is then written once from it.
    """
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    if match.release is None:
        raise RuntimeError(f"{match.path.name} is not an AmigaOS CD")
    written = 0
    with iso9660.IsoImage.open(match.path) as iso:
        for layer in sorted(match.present, key=lambda item: item.order):
            root = iso.find(layer.source)
            if root is None:
                continue
            destination = into / layer.destination if layer.destination else into
            count = 0
            for relative, entry in iso.walk(root):
                if entry.is_dir:
                    (destination / relative).mkdir(parents=True, exist_ok=True)
                    continue
                iso.extract(entry, destination / relative)
                count += 1
            written += count
            if progress is not None:
                progress.log(f"  {layer.label}: {count} files -> "
                             f"{layer.destination or 'the root of the drive'}")
    return written


def requirements(release: Release, machine: machines.Machine,
                 accelerator: machines.Accelerator,
                 card_cpu: Cpu | None = None,
                 kickstart_version: int | None = None) -> list[str]:
    """What stops this release running on this machine, in plain words.

    An empty list means nothing does.  Both checks are made every time even
    though one of them cannot currently fail on a PiStorm: the processor
    requirement belongs to AmigaOS rather than to today's accelerator, and
    stating it here is what makes the machine model answer the question rather
    than a comment somewhere describing why it was not asked.
    """
    problems: list[str] = []

    cpu = machine.cpu_fitted(accelerator, card_cpu)
    if not cpu.at_least(release.needs_cpu):
        problems.append(
            f"{release.label} needs a {release.needs_cpu.label} or better, and "
            f"{machine.label} with a {accelerator.label.lower()} has a "
            f"{cpu.label}.")

    #  A Kickstart is only wrong once one has been chosen; not having chosen
    #  yet is a different complaint and belongs to whatever asks for one.
    if kickstart_version is not None and kickstart_version != NEEDS_KICKSTART:
        problems.append(
            f"{release.label} needs Kickstart 3.1 (V{NEEDS_KICKSTART}), and "
            f"the chosen ROM is V{kickstart_version}.")

    return problems
