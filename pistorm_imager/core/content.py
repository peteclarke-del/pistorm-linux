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


def discover(folder: str | Path) -> list[Category]:
    """The categories a content folder is divided into.

    Looks for a container drawer - WHDLOAD is the usual one - and lists what
    is inside it. A tree with no such drawer has no categories to offer, which
    is different from having none this machine can run.
    """
    root = Path(folder)
    if not root.is_dir():
        return []
    found: list[Category] = []
    seen: set[str] = set()
    for container in CONTAINERS:
        try:
            entries = [p for p in (root / container).iterdir() if p.is_dir()]
        except OSError:
            continue
        #  The real name on disk, whatever its case, is what the copy matches.
        actual = (root / container).name if (root / container).exists() else container
        for entry in sorted(entries, key=lambda p: p.name.lower()):
            key = _folder_key(entry.name)
            if not key or key in seen:
                continue
            seen.add(key)
            needs, note = KNOWN.get(key, (None, ""))
            try:
                count = sum(1 for _ in entry.iterdir())
            except OSError:
                count = 0
            found.append(Category(f"{actual}/{entry.name}", entry.name,
                                  needs, note, count))
        if found:
            break
    return found


def unsuitable(categories: list[Category], machine: Machine) -> list[str]:
    """The paths this machine cannot run, as a starting point for exclusions."""
    return [c.path for c in categories if not c.suits(machine)]
