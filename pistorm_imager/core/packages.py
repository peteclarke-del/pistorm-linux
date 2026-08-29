"""Optional software to add to a Workbench installed from floppy images.

A Workbench built from the original disks is exactly what shipped in 1994: no
archiver, no installer, and no idea what WHDLoad is. The pieces almost everyone
adds next are listed here.

None of it is shipped with this project - it belongs to its authors - so each
package is copied out of a system you already have. An emulator installation
such as PiMiga carries all of them, and so does any Workbench you have already
set up; point at its System drive and whatever is there becomes available.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Package:
    key: str
    label: str
    description: str
    #  (path within the donor system, destination within the target drive).
    #  A source that is a directory is copied whole; a file is copied into the
    #  destination drawer.
    items: tuple[tuple[str, str], ...]
    default: bool = False
    rtg_only: bool = False


CATALOGUE: list[Package] = [
    Package(
        "whdload", "WHDLoad",
        "Runs floppy games and demos from the hard drive. Almost every game "
        "collection is built around it.",
        (("C/WHDLoad", "C"), ("Expansion/WHDLoad", "Expansion/WHDLoad")),
        default=True,
    ),
    Package(
        "lha", "LhA",
        "The archiver Amiga software is distributed in. Without it very little "
        "downloaded from Aminet can be unpacked.",
        (("C/lha", "C"),),
        default=True,
    ),
    Package(
        "installer", "Installer",
        "Commodore's installer, which most third-party install scripts expect "
        "to find and fail without.",
        (("C/Installer", "C"),),
        default=True,
    ),
    Package(
        "igame", "iGame",
        "A launcher that lists WHDLoad games with their screenshots.",
        (("Programs/iGame", "Programs/iGame"),),
    ),
    Package(
        "picasso96", "Picasso96",
        "The RTG subsystem. Only useful where there is an RTG display to draw "
        "on - the Pi's HDMI output.",
        (("Libs/Picasso96", "Libs/Picasso96"),
         ("Prefs/Picasso96Mode", "Prefs"),
         ("Libs/rtg.library", "Libs")),
        rtg_only=True,
    ),
]

CATALOGUE_BY_KEY = {p.key: p for p in CATALOGUE}


def donor_system(folder: str | Path) -> Path | None:
    """Find a Workbench system drive to copy packages out of.

    Accepts the drive itself, or a PiMiga folder, in which case its System
    drive is used.
    """
    folder = Path(folder)
    for candidate in (folder, folder / "System", folder / "disks" / "System",
                      folder / "pimiga" / "disks" / "System"):
        #  A C drawer is what makes something a system drive; whether it holds
        #  any particular package is checked per package afterwards.
        if (candidate / "C").is_dir():
            return candidate
    return None


def available(donor: str | Path | None) -> dict[str, list[str]]:
    """Which packages this donor can supply, and what each is missing."""
    system = donor_system(donor) if donor else None
    if system is None:
        return {}
    found: dict[str, list[str]] = {}
    for package in CATALOGUE:
        missing = [source for source, _dest in package.items
                   if not (system / source).exists()]
        #  A package is offered when at least its first item is there; the rest
        #  are extras that some installations arrange differently.
        if len(missing) < len(package.items):
            found[package.key] = missing
    return found


def overlays_for(donor: str | Path, keys: list[str],
                 rtg: bool = True) -> list[tuple[str, str]]:
    """Turn chosen packages into (source, destination) pairs to copy."""
    system = donor_system(donor)
    if system is None:
        return []
    out: list[tuple[str, str]] = []
    for key in keys:
        package = CATALOGUE_BY_KEY.get(key)
        if package is None or (package.rtg_only and not rtg):
            continue
        for source, destination in package.items:
            path = system / source
            if path.exists():
                out.append((str(path), destination))
    return out


def default_keys(rtg: bool = True) -> list[str]:
    return [p.key for p in CATALOGUE if p.default and (rtg or not p.rtg_only)]
