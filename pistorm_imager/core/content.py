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
import re
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
#  Drawers AmigaOS, Workbench or the card itself own. A duplicate found
#  inside one of these is never offered, because what would be removed is
#  the drawer, and "Delete SYS:C ALL" is the end of AmigaDOS.
SYSTEM_DRAWERS_LOWER = {
    "c", "s", "l", "libs", "devs", "prefs", "fonts", "locale", "utilities",
    "tools", "system", "wbstartup", "storage", "classes", "expansion",
    "rexxc", "rexx", "trashcan", "t", "temp", "programs", "internet",
    "audio", "games", "demos", "wbgames", "icons", "myfiles",
    "storage/install", "locale/catalogs", "prefs/env-archive",
    "devs/monitors", "devs/dosdrivers", "devs/networks", "libs/mui",
}

SOFTWARE_DRAWERS = ("Programs", "WBGames", "Internet", "Audio", "Extras")


#  An Amiga binary carries its version in a "$VER:" string. It is the only
#  evidence about age that is actually in the file, so it is what decides
#  whether a copy on a drive is older than the one a package installs.
VER_STRING = re.compile(rb"\$VER:? ?([ -~]{3,60})")
VERSION_NUMBER = re.compile(r"(\d+)\.(\d+)")


def version_of(data: bytes) -> tuple[int, int] | None:
    """The (version, revision) a binary claims, or None if it claims none."""
    for raw in VER_STRING.findall(data[:200000]):
        text = raw.decode("latin-1")
        found = VERSION_NUMBER.search(text)
        if found:
            return int(found.group(1)), int(found.group(2))
    return None


@dataclasses.dataclass(frozen=True)
class Duplicate:
    """A copy of chosen software already on the drive, somewhere else."""
    package: str            # the package key that installs it
    label: str              # what to call it on screen
    program: str            # the file name that matched
    where: str              # full path of the copy found on the drive
    drawer: str             # what would be removed - the drawer holding it
    theirs: tuple[int, int] | None
    ours: tuple[int, int] | None

    @property
    def certain(self) -> bool:
        """Whether ours is provably newer, and so safe to offer by default.

        Both versions have to be readable and ours strictly greater. Anything
        else is a question rather than an answer: ClassicWB's System/FBlit
        holds the *same* FBlit build as the package, plus an FBlitGUI the
        package does not ship, so removing it on a name match alone would
        take a program away.
        """
        return bool(self.ours and self.theirs and self.ours > self.theirs)


def list_files(reader, limit: int = 40000) -> list[tuple[str, object]]:
    """Every file on a volume, once, so a repeated search need not walk again.

    Walking a system drive is a few seconds. Doing it again each time a
    package is ticked would make the list unusable, so the caller keeps this
    and passes it back.
    """
    out = []
    for path, entry in reader.walk():
        if len(out) >= limit:
            break
        if not entry.is_dir:
            out.append((path, entry))
    return out


def find_duplicates(reader, wanted: dict[str, tuple[str, str, tuple | None]],
                    filling: Iterable[str] = (),
                    listing: list[tuple[str, object]] | None = None,
                    limit: int = 40000) -> list[Duplicate]:
    """Copies of ``wanted`` already on this drive, in some other place.

    ``wanted`` maps a program's file name to (package key, label, our
    version). Nothing is named in the source: the caller works the names out
    from what the chosen packages install, and this looks for them.

    Only files, only outside the place the package installs to - a copy in
    the same place is an older *file*, which displacement already replaces -
    and only where the drawer holding it is not one the system owns.
    """
    ours_too = [d.strip("/").lower() for d in filling if d.strip("/")]
    found: dict[str, Duplicate] = {}
    for path, entry in (listing if listing is not None
                        else list_files(reader, limit)):
        key = wanted.get(entry.name.lower())
        if key is None:
            continue
        package, label, ours = key
        drawer = path.rpartition("/")[0]
        lowered = drawer.lower()
        #  The drawer has to be *about* this program, which means named for
        #  it. What goes is the whole drawer, and a program sitting inside
        #  somebody else's is not a duplicate of anything: matching on the
        #  file alone offered to delete Programs/DiskSalv because Picasso96
        #  ships an "Installer" and DiskSalv's drawer has one too, and
        #  Tools/Commodities - Exchange, Blanker, CrossDOS and the rest -
        #  because Commodore's ClickToFront commodity lives in it.
        if lowered.rpartition("/")[2] != entry.name.lower():
            continue
        #  Never offer a drawer the system owns: that is how a duplicate in
        #  C or Libs would take AmigaDOS with it.
        if not drawer or lowered in SYSTEM_DRAWERS_LOWER:
            continue
        #  Nor one this build is itself filling, or anything inside it. Our
        #  MUI overlay merges into the drive's own System/MUI, so every class
        #  in it matches by name and none of them is a duplicate.
        if any(lowered == d or lowered.startswith(d + "/") for d in ours_too):
            continue
        try:
            theirs = version_of(reader.read_file(entry))
        except Exception:                                   # noqa: BLE001
            theirs = None
        #  Some evidence is required. Removing a drawer whole on a name
        #  match alone is how ClassicWB's System/FBlit - the same FBlit
        #  build as the package, plus an FBlitGUI it does not ship - would
        #  be taken away.
        if theirs is None:
            continue
        candidate = Duplicate(package=package, label=label,
                              program=entry.name, where=path, drawer=drawer,
                              theirs=theirs, ours=ours)
        #  One row per drawer: five matches inside Programs/DirOpus4 are one
        #  older copy, and the drawer is what would go.
        best = found.get(lowered)
        if best is None or (candidate.certain and not best.certain):
            found[lowered] = candidate
    return sorted(found.values(), key=lambda d: (not d.certain, d.where))


#  Assigns and devices AmigaOS provides, or that this tool always makes. A
#  script naming one of these is not asking for anything unusual.
STOCK_VOLUMES = {
    "sys", "c", "s", "l", "libs", "devs", "fonts", "locale", "env", "envarc",
    "t", "ram", "progdir", "rexx", "classes", "help", "keymaps", "printers",
    "storage", "prefs", "clipboard", "nil", "con", "raw", "ser", "par", "prt",
    "df0", "df1", "df2", "df3", "mui", "in", "out", "aux", "speak",
}

#  Documentation, not instructions. A guide explaining how to mount PC: is
#  not a script that tries to, and quoting one as evidence is how a check
#  like this stops being believed.
NOT_A_SCRIPT = (".guide", ".doc", ".txt", ".readme", ".info", ".history",
                ".ct", ".cd", ".nfo", ".me", ".man", ".hlp")

MOUNTS = re.compile(r"(?i)\bmount\s+([A-Za-z0-9_.-]+):")
ASSIGNS_TO = re.compile(
    r"(?i)\bassign\s+(?:>nil:\s+)?(?:add\s+)?[A-Za-z0-9_.-]+:\s+"
    r"([A-Za-z0-9_.-]+):")
#  A startup script that runs another one. Following these is the only way
#  to see the assigns a distribution really makes.
RUNS_SCRIPT = re.compile(r"(?i)^\s*(?:c:)?execute\s+(S:[A-Za-z0-9_.-]+)")

MAKES_ASSIGN = re.compile(
    r"(?i)^\s*(?:c:)?assign\s+(?:>nil:\s+)?(?:add\s+)?([A-Za-z0-9_.-]+):")


@dataclasses.dataclass(frozen=True)
class Broken:
    """Software on the drive that cannot work on the card being built."""
    drawer: str
    reasons: tuple[str, ...]


#  Where a program that opens files is likely to live. A default tool with
#  no path in front of it has to be given one, and this is where to look.
TOOL_DRAWERS = ("C", "Utilities", "System", "Tools", "Prefs")


def programs_by_name(reader) -> dict[str, str]:
    """Every runnable program on the drive, by name, as a full path.

    Used to give a default tool its path. Workbench runs the tool named in an
    icon, and a bare name with no path in front of it is not found the way a
    shell would find it - which is why ClassicWB's def_project.info, whose
    tool is simply "MultiView", opens nothing at all.
    """
    out: dict[str, str] = {}
    for drawer in TOOL_DRAWERS:
        entry = reader.find(drawer)
        if entry is None or not entry.is_dir:
            continue
        try:
            inside = reader.listdir(_locator(entry))
        except Exception:                                   # noqa: BLE001
            continue
        for child in inside:
            if child.is_dir:
                continue
            #  An icon with no program beside it still says where the program
            #  is meant to be. ClassicWB's Utilities holds MultiView.info and
            #  no MultiView: it expects the Workbench floppies to supply one,
            #  and this build does - but not until after these icons have
            #  been copied, so the name has to be learned from the icon.
            name = child.name[:-5] if child.name.endswith(".info") else child.name
            if not name:
                continue
            here = f"SYS:{drawer}/{name}"
            if child.name.endswith(".info"):
                out.setdefault(name.lower(), here)
            else:
                out[name.lower()] = here          # a real file wins
    return out


def volumes_on_the_card(reader, named: Iterable[str] = ()) -> set[str]:
    """Every volume and assign a script could reasonably expect to find.

    The drives this build makes, whatever the drive assigns for itself in
    its own startup files, and what AmigaOS provides. Read rather than
    assumed: a distribution makes its own assigns, and calling those missing
    would condemn most of what it ships.
    """
    out = {str(n).strip(":").lower() for n in named if str(n).strip(":")}
    out |= STOCK_VOLUMES
    #  ...and whatever those scripts run in turn. ClassicWB's User-Startup
    #  does "Execute S:Assign-Startup", and that is where A-Programs:,
    #  A-Games: and the rest are made - so reading only the two obvious
    #  files said those volumes did not exist and called perfectly good
    #  software broken.
    seen: set[str] = set()
    queue = ["S/Startup-Sequence", "S/User-Startup"]
    while queue:
        path = queue.pop(0)
        if path.lower() in seen:
            continue
        seen.add(path.lower())
        entry = reader.find(path)
        if entry is None or getattr(entry, "is_dir", False):
            continue
        try:
            text = reader.read_file(entry).decode("latin-1", "replace")
        except Exception:                                   # noqa: BLE001
            continue
        for line in text.splitlines():
            found = MAKES_ASSIGN.match(line)
            if found:
                out.add(found.group(1).lower())
            runs = RUNS_SCRIPT.match(line)
            if runs and len(seen) < 20:
                queue.append(runs.group(1).replace("S:", "S/"))
    return out


def cannot_work(reader, volumes: Iterable[str],
                dosdrivers: Iterable[str] = ()) -> list[Broken]:
    """Programs on the drive that cannot run on the card being built.

    A ready-made distribution carries software written for the machine it
    was assembled on. ClassicWB's FMSsys is the example: its MountFMS does
    "assign FMS: A-Programs:FMSsys" and "mount FF0:", and this card has
    neither an A-Programs: volume nor a DEVS:DOSDrivers/FF0 - so it asks a
    question and then fails, every time, and nothing says why.

    Only what can be shown from the files: a binary for another processor,
    a script mounting a device this card has not got, or one needing a
    volume that will not exist. Everything else is left alone.
    """
    known = {str(v).strip(":").lower() for v in volumes}
    drivers = {str(d).lower() for d in dosdrivers}
    out: list[Broken] = []
    for drawer, name in installed_programs(reader):
        where = f"{drawer}/{name}"
        entry = reader.find(where)
        if entry is None or not entry.is_dir:
            continue
        why: list[str] = []
        try:
            inside = reader.listdir(_locator(entry))
        except Exception:                                   # noqa: BLE001
            continue
        for kid in inside:
            if kid.is_dir or kid.name.lower().endswith(NOT_A_SCRIPT):
                continue
            try:
                data = reader.read_file(kid)
            except Exception:                               # noqa: BLE001
                continue
            if data[:4] == b"\x7fELF":
                why.append(f"{kid.name} is built for another processor")
                continue
            #  Only small text files: a script, not a program or a payload.
            if data[:4] == b"\x00\x00\x03\xf3" or len(data) > 20000:
                continue
            text = data.decode("latin-1", "replace")
            for found in MOUNTS.finditer(text):
                device = found.group(1)
                if device.lower() not in drivers:
                    why.append(f"{kid.name} mounts {device}:, and this card "
                               f"has no DEVS:DOSDrivers/{device}")
            for found in ASSIGNS_TO.finditer(text):
                volume = found.group(1)
                if volume.lower() not in known:
                    why.append(f"{kid.name} needs the volume {volume}:, "
                               f"which this card has not got")
        if why:
            out.append(Broken(where, tuple(dict.fromkeys(why))))
    return out


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
