"""Add-ons installed onto the Emu68 boot partition rather than onto Workbench.

Everything in :mod:`packages` goes onto an Amiga drive and is software the
Amiga runs. This is the other kind: something that belongs on the **FAT32 boot
partition**, beside the Emu68 kernel and ``config.txt``, and is finished on the
Amiga by its own installer afterwards.

The first of them is AGA-PISTORM, which ships a patched Emu68 kernel and the
tools to switch an OCS or ECS machine onto an emulated AGA chipset while a
WHDLoad game runs. Its own instructions have two steps, and the first of them
is **a Windows batch file**: it finds the card's boot partition, backs up
``config.txt``, makes the partition writable from the Amiga, adds the Emu68
overlays the card is missing and copies the add-on's drawer onto it. Every one
of those is something this tool already does, and a Linux user currently has
to find a Windows PC to have them done. That is the gap this closes.

**What this does not do.** The second step - installing the kernel, its
``config.txt`` line, the replacement RTG driver and the Workbench menu - is the
add-on's own ``Install``, run on the Amiga, and it stays there. It checks the
machine it is running on, asks questions this tool cannot answer for it
(whether there is a Framethrower, whether the Agnus is PAL or NTSC), and backs
up everything it replaces. Doing that from here would mean reimplementing an
installer whose author tests it and this does not, on a card whose Workbench
this tool may not have built - which is how an imager introduces exactly the
incompatibility it exists to avoid.

**Nothing here is downloaded.** These are published where a browser is needed
to get them - itch.io's "name your own price" page will not serve a file to
anything else - so the archive is one the user has, found wherever they keep
Amiga material, exactly as a Kickstart or a PFS3 handler is. What is *not*
done is downloading a login page and caching it as though it were the archive.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from .machines import Accelerator, Chipset, Machine, Pi
from .util import Progress, human_size

#  What a backed-up file is called.  The add-on's own installer uses the same
#  suffix for everything it replaces on the Amiga side, and a person putting a
#  card back together by hand should find one convention, not two.
BACKUP_SUFFIX = "pre-aga"

#  Where an Emu68 boot partition keeps its device tree overlays.  Emu68 1.1
#  ships a drawer of them; 1.0.7 has none at all, which is one of several
#  reasons these add-ons want 1.1.
OVERLAYS = "overlays"


@dataclasses.dataclass(frozen=True)
class BootAddon:
    """Something installed onto the boot partition, and what it needs.

    Everything here is either a fact about the add-on or a requirement it
    states.  No path is written down that the publisher might renumber: the
    drawer is found by **name** anywhere inside whatever was downloaded,
    because the wrapping around it changes between releases and the thing
    itself does not.
    """

    key: str
    label: str
    description: str
    #  Where to get it, said to somebody who has to fetch it themselves.
    home: str
    #  What the download is called, as a glob.  The publisher puts the version
    #  and the supported machines in the file name and both change, so this
    #  matches the part that does not.
    archive: str
    #  The drawer inside the download that goes onto the boot partition, by
    #  name.  Its icon - ``<drawer>.info`` - goes with it, because a drawer
    #  with no icon cannot be opened from Workbench, which is step 2.
    drawer: str
    #  The drawer inside it holding Emu68 overlays, if any.  These are copied
    #  into the boot partition's own overlays drawer where it has not got
    #  them, so the add-on's installer has something to enable.
    overlays: str = ""
    #  Files backed up before anything changes them.
    backs_up: tuple[str, ...] = ("config.txt",)
    #  Whether the Amiga has to be able to write to the boot partition. Emu68
    #  1.1 mounts it read-only unless told otherwise, and an installer run on
    #  the Amiga that cannot write to the partition it is installing from
    #  fails at the last step.
    writable_boot: bool = False

    #  ---- what it needs of the hardware -------------------------------
    #  Chipsets it is for. AGA emulation is pointless on a machine that has
    #  AGA, so this refuses rather than merely not recommending.
    chipsets: tuple[Chipset, ...] = ()
    #  Raspberry Pi models; empty means any.
    pi_models: tuple[Pi, ...] = ()
    #  The oldest Emu68 this works with, as (major, minor). A soft gate: a
    #  build from a local zip or an unpacked folder carries no version for
    #  anything to read, and refusing what cannot be checked would hide the
    #  add-on from everybody building that way.
    min_emu68: tuple[int, ...] = ()
    #  Chip RAM in KB. A real requirement that a real machine fails: an
    #  unexpanded A500 has 512K and this wants a megabyte.
    min_chip_ram: int = 0
    #  A prepared system it expects to find on the Amiga side, as a
    #  distributions key. Warned about rather than refused - the Amiga drive
    #  may have been set up long before this card was built, and this tool
    #  cannot see inside every one of them.
    wants_distribution: str = ""
    #  Things that are true and cannot be checked from here.
    notes: tuple[str, ...] = ()

    def suits(self, machine: Machine, *, accelerator: Accelerator,
              pi: Pi | None = None, chip_ram: int = 0,
              emu68_tag: str | None = None) -> bool:
        """Whether this add-on can work on the card being built."""
        from . import emu68 as emu68_module                   # noqa: PLC0415
        if accelerator is not Accelerator.PISTORM:
            return False
        if self.chipsets and machine.chipset not in self.chipsets:
            return False
        if self.pi_models and pi not in self.pi_models:
            return False
        if self.min_chip_ram and chip_ram < self.min_chip_ram:
            return False
        if self.min_emu68 and not emu68_module.at_least(emu68_tag or "",
                                                        self.min_emu68):
            return False
        return True

    def refusal(self, machine: Machine, *, accelerator: Accelerator,
                pi: Pi | None = None, chip_ram: int = 0,
                emu68_tag: str | None = None) -> str:
        """Why it cannot be had here, in words, or "" when it can.

        Said rather than implied.  A switch that is simply greyed out is a
        question - "why can I not turn this on?" - and the answer is known
        exactly here, so it is given.
        """
        from . import emu68 as emu68_module                   # noqa: PLC0415
        from .machines import chip_ram_label                  # noqa: PLC0415
        if accelerator is not Accelerator.PISTORM:
            return "needs a PiStorm; it is an Emu68 kernel."
        if self.chipsets and machine.chipset not in self.chipsets:
            wanted = " or ".join(c.value for c in self.chipsets)
            return (f"is for {wanted} machines; {machine.label} has "
                    f"{machine.chipset.value}.")
        if self.pi_models and pi not in self.pi_models:
            wanted = " or ".join(model.label for model in self.pi_models)
            return f"needs {wanted} on the board."
        if self.min_chip_ram and chip_ram < self.min_chip_ram:
            return (f"needs {chip_ram_label(self.min_chip_ram)} of chip RAM; "
                    f"this card is being built for "
                    f"{chip_ram_label(chip_ram)}.")
        if self.min_emu68 and not emu68_module.at_least(emu68_tag or "",
                                                        self.min_emu68):
            version = ".".join(str(part) for part in self.min_emu68)
            return (f"needs Emu68 {version} or newer; choose one on the "
                    f"Source page.")
        return ""


CATALOGUE: list[BootAddon] = [
    BootAddon(
        "agapistorm", "AGA-PISTORM",
        "Runs AGA WHDLoad games - the ones written for the A1200 and A4000 - "
        "on an OCS or ECS machine. The Pi emulates Alice, Lisa and Paula on a "
        "spare core while a game runs and hands the chipset back when it "
        "quits. This does the part its instructions ask a Windows PC for; the "
        "Install in the drawer is then double-clicked on the Amiga.",
        home="https://astair86.itch.io/aga-pistorm-10-experimental-prototype",
        #  "AGA-Pistorm universal(1MB_2MB chip)_(Pistorm_Classic_16_600)v1.1.7z"
        #  today. The version and the machine list in that name have both
        #  changed once already.
        archive="AGA-Pistorm*",
        drawer="AGA-Pistorm",
        overlays="Overlays",
        writable_boot=True,
        chipsets=(Chipset.OCS, Chipset.ECS),
        #  A Pi 3 and a Pi 4 are both supported, so no model is refused.
        min_emu68=(1, 1),
        #  "1 or 2 MB of Chip RAM - for example an A500 with a 2 MB Agnus
        #  upgrade, or an A500+ with its 1/2 MB upgrade."
        min_chip_ram=1024,
        wants_distribution="caffeineos",
        notes=(
            "Back up the card's boot partition before installing it. The "
            "Amiga side replaces the Emu68 kernel and the RTG driver.",
            "Only WHDLoad games switch to the AGA chipset. A game started "
            "from Workbench, or from an ADF, runs on the Amiga's own chipset "
            "and will look wrong.",
            "A Raspberry Pi 4 on a classic PiStorm needs the EPM240 "
            "longer-hold CPLD firmware flashed to the board. That is a "
            "hardware job and nothing here can do it or check it - a card "
            "already running CaffeineOS on a Pi 4 has it.",
            "Finish it on the Amiga: open the boot partition, open the "
            "drawer, double-click Install, then power the machine off and on. "
            "A Ctrl-Amiga-Amiga reset does not load a new kernel.",
            "Experimental, and its author says so: one tested machine, and "
            "graphical glitches in some games.",
        ),
    ),
]

CATALOGUE_BY_KEY = {a.key: a for a in CATALOGUE}


# ------------------------------------------------------------- finding it

def search_roots(extra: list[str] | None = None) -> list[Path]:
    """Where to look for an archive the user has downloaded themselves.

    The same places the tool already looks for a Kickstart or a PFS3 handler,
    plus the package cache, so somebody who has put one beside their other
    Amiga material does not have to be told about a second folder.
    """
    from . import emu68, packages, presets                    # noqa: PLC0415
    roots = list(presets._search_roots(extra))                # noqa: SLF001
    roots.append(packages.cache_dir())
    roots.append(emu68.cache_dir())
    return [root for root in roots if root.is_dir()]


def find_archive(addon: BootAddon,
                 extra: list[str] | None = None) -> Path | None:
    """The newest download matching this add-on, or ``None``.

    Newest by modification time rather than by the version in the name: the
    publisher's naming has changed between releases and a version read out of
    a file name would have to be parsed from whatever they chose this time.
    """
    found: list[Path] = []
    for root in search_roots(extra):
        try:
            found += [path for path in root.glob(addon.archive)
                      if path.is_file()]
        except OSError:
            continue
    if not found:
        return None
    return max(found, key=lambda path: path.stat().st_mtime)


def locate_drawer(root: Path, addon: BootAddon) -> Path | None:
    """The add-on's drawer inside an unpacked download, found by name.

    Walked for rather than named by path.  Today it is at
    ``Amiga/AGA-Pistorm``, with the Windows script beside it and the source in
    another drawer; that arrangement is the publisher's and has no reason to
    hold. What does hold is the name of the drawer that goes onto the card,
    because the Amiga side opens it by that name.
    """
    wanted = addon.drawer.lower()
    best: Path | None = None
    for current, drawers, _files in os.walk(root):
        for name in drawers:
            if name.lower() == wanted:
                here = Path(current) / name
                #  The shallowest match wins: an archive that also carries its
                #  own source tree can hold the name twice.
                if best is None or len(here.parts) < len(best.parts):
                    best = here
    return best


# ------------------------------------------------------------ installing

def _tree_size(folder: Path) -> int:
    return sum(path.stat().st_size
               for path in folder.rglob("*") if path.is_file())


def _copy_tree(fs, source: Path, destination: str,
               progress: Progress) -> list[str]:
    """Copy a folder onto the FAT32 boot partition, keeping its shape."""
    written: list[str] = []
    fs.makedirs(destination)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = f"{destination}/{str(relative).replace(os.sep, '/')}"
        if path.is_dir():
            fs.makedirs(target)
        elif path.is_file():
            fs.write_file(target, path)
            written.append(target)
    progress.log(f"  {destination}/ - {len(written)} files")
    return written


def install(fs, addon: BootAddon, unpacked: Path,
            progress: Progress) -> list[str]:
    """Do on the boot partition what the add-on's PC step does.

    Returns the paths written.  Raises ``ValueError`` when the download is not
    what it claims to be or will not fit, because a half-installed boot
    partition is worse than one that was left alone.
    """
    drawer = locate_drawer(unpacked, addon)
    if drawer is None:
        raise ValueError(
            f"{addon.label}: no {addon.drawer} drawer inside the archive, so "
            f"this is not the download it expects")

    needed = _tree_size(drawer)
    free = fs.free_bytes
    if needed > free:
        raise ValueError(
            f"{addon.label} needs {human_size(needed)} on the boot partition "
            f"and there is {human_size(free)}. Make the boot partition larger "
            f"on the Target page and build again.")

    written: list[str] = []

    #  Backups first, and once.  Re-running must never overwrite the original
    #  backup with a file that has already been changed - that is how somebody
    #  loses the config.txt they are meant to be able to put back.
    for name in addon.backs_up:
        backup = f"{name}.{BACKUP_SUFFIX}"
        if not fs.exists(name):
            continue
        if fs.exists(backup):
            progress.log(f"  {backup} is already there and is left alone")
            continue
        fs.write_bytes(backup, fs.read_bytes(name))
        written.append(backup)
        progress.log(f"  {name} backed up as {backup}")

    #  The drawer and its icon.  Without the icon the drawer cannot be opened
    #  from Workbench, which is the whole of step 2.
    written += _copy_tree(fs, drawer, addon.drawer, progress)
    icon = drawer.with_name(drawer.name + ".info")
    if icon.is_file():
        fs.write_file(f"{addon.drawer}.info", icon)
        written.append(f"{addon.drawer}.info")
        progress.log(f"  {addon.drawer}.info")
    else:
        progress.log(f"  WARNING: {addon.drawer}.info is not in the archive, "
                     f"so the drawer will have no icon on Workbench")

    #  The overlays the card has not got.  Never the ones it has: those came
    #  with the Emu68 release this card was built from and are the ones its
    #  kernel was built against.
    source = locate_drawer(drawer, dataclasses.replace(
        addon, drawer=addon.overlays)) if addon.overlays else None
    if source is not None:
        fs.makedirs(OVERLAYS)
        have = {entry.name.lower() for entry in fs.listdir(OVERLAYS)}
        added = 0
        for path in sorted(source.glob("*.dtbo")):
            if path.name.lower() in have:
                continue
            fs.write_file(f"{OVERLAYS}/{path.name}", path)
            written.append(f"{OVERLAYS}/{path.name}")
            added += 1
        progress.log(f"  {OVERLAYS}/ - {added} overlay(s) the card did not "
                     f"have" if added else
                     f"  {OVERLAYS}/ - the card already has all of them")
    elif addon.overlays:
        progress.log(f"  WARNING: no {addon.overlays} drawer in the archive")

    return written
