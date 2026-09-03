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
        loose = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        loose = []
    for entry in sorted(loose, key=lambda p: p.name.lower()):
        if entry.name.lower() in containers or entry.name.startswith("."):
            continue
        needs, note = needs_from_name(entry.name)
        found.append(Category(entry.name, entry.name, needs, note,
                              _count(entry)))
    return found


def _count(folder: Path) -> int:
    try:
        return sum(1 for _ in folder.iterdir())
    except OSError:
        return 0


def unsuitable(categories: list[Category], machine: Machine) -> list[str]:
    """The paths this machine cannot run, as a starting point for exclusions."""
    return [c.path for c in categories if not c.suits(machine)]
