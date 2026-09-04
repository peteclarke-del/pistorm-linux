"""What a games or demos tree is divided into, and what this machine can run.

A WHDLoad collection is arranged by category - AGA, CD32, CDTV, NTSC and so on
- and not every category suits every Amiga. Putting the AGA games on an OCS
A500 wastes gigabytes on titles that cannot run, and leaves iGame offering
them.

The list of categories is discovered from the tree itself rather than fixed
here, because collections differ and grow: PiMiga's Games drawer has ten,
its Demos drawer four, and another collection will have others. What is fixed
is what a handful of well-known names *mean*, so that a sensible default can be
proposed for the machine. Anything unrecognised is offered too, simply with
nothing assumed about it.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from pathlib import Path

from .machines import Chipset, Machine

#  Category names whose hardware requirement is known.  Matched on the folder
#  name, case-insensitively; anything else is listed with no requirement, which
#  means it is never excluded by default.
KNOWN: dict[str, tuple[Chipset | None, str]] = {
    "aga": (Chipset.AGA, "Titles that need AGA and will not run on OCS or ECS"),
    "cd32": (Chipset.AGA, "CD32 titles, which are AGA machines"),
    "aga-cd32": (Chipset.AGA, "AGA and CD32 titles"),
    "cdtv": (None, "CDTV titles - an OCS machine, so these run anywhere"),
    "ocs": (None, "Titles for the original chipset, which every Amiga runs"),
    "ecs": (Chipset.ECS, "Titles that need ECS"),
    "ntsc": (None, "NTSC titles - they run, but at 60Hz on a PAL machine"),
    "arcadia": (None, "Arcadia arcade system titles"),
    "cinemaware": (None, "The Cinemaware catalogue"),
    "mags": (None, "Disk magazines"),
    "cracktros": (None, "Crack intros"),
    "foreign": (None, "Titles in languages other than English"),
    "beta": (None, "Unfinished or beta releases"),
}

#  Where a collection keeps its categories, relative to the drive's root.
CONTAINERS = ("WHDLOAD", "WHDLoad", "Games", "Demos")


@dataclasses.dataclass(frozen=True)
class Category:
    """One division of a collection, as a path inside the drive."""

    path: str                       # e.g. "WHDLOAD/AGA"
    label: str                      # e.g. "AGA"
    needs: Chipset | None = None
    note: str = ""
    entries: int = 0

    def suits(self, machine: Machine) -> bool:
        """Whether this machine can run what is in here."""
        if self.needs is None:
            return True
        if self.needs is Chipset.AGA:
            return machine.aga
        if self.needs is Chipset.ECS:
            return machine.chipset in (Chipset.ECS, Chipset.AGA)
        return True


def _folder_key(name: str) -> str:
    return name.strip().lower().removesuffix(".info")


#  Markers a title puts in its own name to say which chipset it wants. Matched
#  in UPPER CASE and only at a word boundary, which is what keeps "Saga" and
#  "Vagabond" out of it - their "aga" is lower case and mid-word.
NAME_MARKERS = (("CD32", Chipset.AGA, "the name says CD32, which is an AGA "
                                      "machine"),
                ("AGA", Chipset.AGA, "the name says AGA"))


def needs_from_name(name: str) -> tuple[Chipset | None, str]:
    """What a title's own name admits about the chipset it needs.

    This is the only honest automatic judgement available. Scanning the
    program for AGA-only registers was tried and abandoned: matching 16-bit
    words finds FMODE inside DOOM1.WAD and inside an IFF picture, so it
    labels data as code and would confidently condemn titles that run
    perfectly well. A name is a weaker signal but it is never a guess.
    """
    stem = Path(name).stem
    for marker, needs, why in NAME_MARKERS:
        at = stem.upper().rfind(marker)
        if at < 0 or stem[at:at + len(marker)] != marker:
            continue
        before = stem[at - 1] if at else ""
        after = stem[at + len(marker):at + len(marker) + 1]
        #  A boundary before it, and nothing lower-case run on after it.
        if before.isupper() and before.isalpha():
            continue
        if after.islower():
            continue
        return needs, why
    return None, ""


def discover(folder: str | Path) -> list[Category]:
    """What a content folder holds, as things that can be left out.

    Two kinds. A collection keeps its titles in a container drawer - WHDLOAD
    is the usual one - divided into categories whose names say what they
    need, and those are listed as before. Everything *else* at the top of the
    drive is a program in its own right, and those were never offered at all:
    a Games drive with forty native titles sitting beside its WHDLOAD drawer
    could only be taken whole, AGA titles and all, onto an ECS machine.
    """
    root = Path(folder)
    if not root.is_dir():
        return []
    found: list[Category] = []
    seen: set[str] = set()
    containers: set[str] = set()

    for container in CONTAINERS:
        here = root / container
        try:
            entries = [p for p in here.iterdir() if p.is_dir()]
        except OSError:
            continue
        containers.add(here.name.lower())
        actual = here.name
        for entry in sorted(entries, key=lambda p: p.name.lower()):
            key = _folder_key(entry.name)
            if not key or key in seen:
                continue
            seen.add(key)
            needs, note = KNOWN.get(key, (None, ""))
            if needs is None and not note:
                needs, note = needs_from_name(entry.name)
            found.append(Category(f"{actual}/{entry.name}", entry.name,
                                  needs, note, _count(entry)))
        if found:
            break

    #  The programs beside the collection, each one its own choice.
    try:
        loose = list(root.iterdir())
    except OSError:
        loose = []
    for entry in sorted(loose, key=lambda p: p.name.lower()):
        if entry.name.lower() in containers or entry.name.startswith("."):
            continue
        needs, note = needs_from_name(entry.name)
        if not entry.is_dir() and (needs is None
                                   or entry.name.lower().endswith(".info")):
            continue
        found.append(Category(entry.name, entry.name, needs, note,
                              _count(entry) if entry.is_dir() else 0))
    return found


def _locator(entry) -> int:
    """Where a directory entry's contents live, whichever reader found it.

    FFS calls it a block and PFS3 calls it an anode; both readers take that
    number back in ``listdir``, so this is the whole of the difference.
    """
    return getattr(entry, "anode", None) or getattr(entry, "block", 0)


#  Where a distribution keeps the software somebody might not want. Only
#  these, and only one level down: a drawer directly inside one of them is a
#  program, while anything deeper is that program's own business, and the
#  system drawers - C, Libs, Devs, S - are not software at all.
#  Not Utilities or Tools: those hold Workbench's own commands, and a list
#  offering to delete Tools/Commodities is a trap rather than a choice. Not
#  Games or Demos either - on a system drive those are the letter drawers a
#  distribution creates for a games partition to be assigned to, and the
#  games themselves are chosen on their own drive.
SOFTWARE_DRAWERS = ("Programs", "WBGames", "Internet", "Audio", "Extras")


def installed_programs(reader) -> list[tuple[str, str]]:
    """What a prepared drive already has installed, as (drawer, program).

    A ready-made distribution arrives with its own idea of what you want -
    ClassicWB FULL carries thirty-one things in Programs alone, some of them
    unfinished, obsolete or simply not to taste - and until now the only
    choice was all of it or none. This lists them so they can be left out one
    at a time.

    Read one drawer at a time rather than by walking the drive: on a volume
    holding twenty gigabytes of games, walking it would take longer than the
    build.
    """
    out: list[tuple[str, str]] = []
    try:
        top = {entry.name.lower(): entry for entry in reader.listdir()
               if entry.is_dir}
    except Exception:                                       # noqa: BLE001
        return out
    for drawer in SOFTWARE_DRAWERS:
        entry = top.get(drawer.lower())
        if entry is None:
            continue
        try:
            inside = reader.listdir(_locator(entry))
        except Exception:                                   # noqa: BLE001
            continue
        for child in sorted(inside, key=lambda e: e.name.lower()):
            #  A drawer, because that is what a program is here. A loose file
            #  in Utilities is one command, and removing it one at a time is
            #  not worth a list sixty rows long.
            if child.is_dir and not child.name.startswith("."):
                out.append((entry.name, child.name))
    return out


def discover_volume(reader) -> list[Category]:
    """The same, read out of an Amiga volume rather than a host folder.

    A drive imported from an .hdf was offered nothing to leave out at all,
    because the listing walked a real directory. The readers for FFS and PFS3
    both list a directory by name, so the same question can be asked of them
    - and it is asked one directory at a time rather than by walking the
    drive, which on twenty gigabytes of games would take longer than the
    build.
    """
    found: list[Category] = []
    seen: set[str] = set()
    containers: set[str] = set()
    try:
        top = reader.listdir()
    except Exception:                            # noqa: BLE001 - unreadable
        return []

    for container in CONTAINERS:
        entry = next((e for e in top if e.is_dir
                      and e.name.lower() == container.lower()), None)
        if entry is None:
            continue
        containers.add(entry.name.lower())
        try:
            inside = [e for e in reader.listdir(_locator(entry)) if e.is_dir]
        except Exception:                        # noqa: BLE001
            inside = []
        for child in sorted(inside, key=lambda e: e.name.lower()):
            key = _folder_key(child.name)
            if not key or key in seen:
                continue
            seen.add(key)
            needs, note = KNOWN.get(key, (None, ""))
            if needs is None and not note:
                needs, note = needs_from_name(child.name)
            found.append(Category(f"{entry.name}/{child.name}", child.name,
                                  needs, note, _volume_count(reader, child)))
        if found:
            break

    for entry in sorted(top, key=lambda e: e.name.lower()):
        if entry.name.lower() in containers or entry.name.startswith("."):
            continue
        needs, note = needs_from_name(entry.name)
        #  Drawers always; a loose file only when its own name says what it
        #  needs. Turrican2AGA on a real drive is a fourteen-byte launcher
        #  rather than a drawer, so a rule about drawers missed the one title
        #  on the whole drive that could be identified - while listing every
        #  file would bury it in save files and icons.
        if not entry.is_dir and needs is None:
            continue
        if not entry.is_dir and entry.name.lower().endswith(".info"):
            continue
        found.append(Category(entry.name, entry.name, needs, note,
                              _volume_count(reader, entry) if entry.is_dir
                              else 0))
    return found


def _volume_count(reader, entry) -> int:
    try:
        return len(reader.listdir(_locator(entry)))
    except Exception:                            # noqa: BLE001
        return 0


def _count(folder: Path) -> int:
    try:
        return sum(1 for _ in folder.iterdir())
    except OSError:
        return 0


#  A launcher names what it runs; it is not the thing itself, so it is small.
LAUNCHER_LIMIT = 512


def _named_inside(data: bytes) -> list[str]:
    """The item names a small text file mentions, if it is text at all."""
    try:
        text = data.decode("latin-1")
    except ValueError:
        return []
    if any(ord(c) < 9 or 13 < ord(c) < 32 for c in text):
        return []                                # not text: a program, a save
    out = []
    for line in text.splitlines():
        #  "AmigaGame.exe", or ":Drawer/Program" from the volume root.
        name = line.strip().lstrip(":").replace("\\", "/").split("/")[0].strip()
        if name:
            out.append(name)
    return out


def followed(excluded: Iterable[str], read_file, present: Iterable[str],
             offered: Iterable[str] = ()) -> list[str]:
    """What else to leave out because only an excluded launcher named it.

    A title can be a few bytes naming the program that runs it - Turrican2AGA
    on a real drive is fourteen bytes reading "AmigaGame.exe" - so leaving the
    launcher out and keeping what it names wastes the space the exclusion was
    meant to save, on something now unreachable.

    Two things stop this doing harm. A launcher that is *kept* pins what it
    names, so a shared engine survives as long as anything still runs it; and
    anything the user was offered as a choice of its own is never taken away
    behind their back.
    """
    excluded = list(excluded)
    gone = {name.lower() for name in excluded}
    offered = {name.lower() for name in offered}
    here = {name.lower(): name for name in present}

    def targets(name: str) -> list[str]:
        data = read_file(name)
        if data is None or len(data) > LAUNCHER_LIMIT:
            return []
        return [here[t.lower()] for t in _named_inside(data)
                if t.lower() in here and t.lower() != name.lower()]

    #  What the survivors still need.
    pinned: set[str] = set()
    for name in present:
        if name.lower() in gone:
            continue
        pinned |= {t.lower() for t in targets(name)}

    extra: list[str] = []
    for name in excluded:
        for target in targets(name):
            key = target.lower()
            if key in gone or key in pinned or key in offered:
                continue
            gone.add(key)
            extra.append(target)
    return extra


def unsuitable(categories: list[Category], machine: Machine) -> list[str]:
    """The paths this machine cannot run, as a starting point for exclusions."""
    return [c.path for c in categories if not c.suits(machine)]
