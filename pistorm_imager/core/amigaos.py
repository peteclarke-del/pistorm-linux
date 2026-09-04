"""Installing AmigaOS onto a partition from Workbench floppy images.

The Windows Emu68 Imager populates a Workbench partition from a set of ADFs;
this does the same job.  Disks are recognised by the *volume name* inside the
image rather than by file name, because ADF collections are named
inconsistently and a renamed file says nothing about its contents.

The destination layout follows what the AmigaOS 3.x install script produces:
the Workbench and Extras disks merge into the root of the drive, while Fonts,
Locale and Storage become drawers of their own.  The Install disk is kept in
its own drawer rather than merged, so that its cut-down C/, L/ and Libs/ cannot
overwrite the full versions from the Workbench disk.
"""
from __future__ import annotations

import dataclasses
import filecmp
import os
import re
import time
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from . import amigafs, amigainfo, compat as compat_module, pfs3, rdb
from .amigafs import Volume, VolumeWriter
from .util import Progress, human_size


@dataclasses.dataclass(frozen=True)
class Role:
    key: str
    label: str
    destination: str        # "" means the root of the drive
    prefixes: tuple[str, ...]
    required: bool = False
    order: int = 50


#  Order matters where disks merge into the same place: the Workbench disk must
#  be copied before Extras so that its versions of shared files win.
ROLES = [
    Role("workbench", "Workbench", "", ("workbench",), required=True, order=10),
    Role("extras", "Extras", "", ("extras",), order=20),
    Role("fonts", "Fonts", "Fonts", ("fonts",), order=30),
    Role("locale", "Locale", "Locale", ("locale",), order=40),
    Role("storage", "Storage", "Storage", ("storage",), order=50),
    Role("classes", "Classes", "Classes", ("classes",), order=55),
    Role("glowicons", "GlowIcons", "", ("glowicons",), order=60),
    Role("backdrops", "Backdrops", "Backdrops", ("backdrops",), order=65),
    Role("install", "Install", "Install", ("install",), order=70),
]


#  "Workbench3.1" / "Extras3.2" carry the release in the volume name; Fonts and
#  Locale do not, so their release has to come from the file name instead.
_VERSION_IN_VOLUME = re.compile(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)$")
_VERSION_IN_FILENAME = re.compile(r"\bv([0-9]+\.[0-9]+(?:\.[0-9]+)?)")


def normalise_version(text: str) -> str:
    """Fold release spellings together: 2.05 and 2.0 are the same release."""
    parts = text.split(".")
    if len(parts) >= 2:
        minor = parts[1].rstrip("0") or "0"
        if len(parts[1]) > 1 and parts[1].startswith("0"):
            minor = "0"
        return f"{parts[0]}.{minor}"
    return text


@dataclasses.dataclass
class DiskMatch:
    path: Path
    volume_name: str
    role: Role | None
    file_count: int
    total_bytes: int
    error: str = ""
    version: str = ""

    @property
    def label(self) -> str:
        if self.error:
            return f"{self.path.name}: {self.error}"
        role = self.role.label if self.role else "unrecognised"
        release = f" {self.version}" if self.version else ""
        return (f'"{self.volume_name}" -> {role}{release} '
                f"({self.file_count} files, {human_size(self.total_bytes)})")

    @property
    def quality(self) -> int:
        """Prefer verified GoodTools dumps, then plain ones, then modified."""
        name = self.path.name
        score = 0
        if "[!]" in name:
            score += 100
        for tag in ("[m", "[a", "[b", "[h", "[o", "[f"):
            if tag in name:
                score -= 10
        if "beta" in name.lower() or "pre-release" in name.lower():
            score -= 50
        return score


def role_for(volume_name: str) -> Role | None:
    lowered = volume_name.strip().lower().replace(" ", "")
    for role in ROLES:
        if any(lowered.startswith(prefix) for prefix in role.prefixes):
            return role
    return None


def version_of(volume_name: str, file_name: str) -> str:
    """Work out which AmigaOS release a disk belongs to."""
    match = _VERSION_IN_VOLUME.search(volume_name.strip().replace(" ", ""))
    if match:
        return normalise_version(match.group(1))
    match = _VERSION_IN_FILENAME.search(file_name)
    if match:
        return normalise_version(match.group(1))
    return ""


def identify(path: str | Path) -> DiskMatch:
    """Read an ADF's volume name and work out which install disk it is."""
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            volume = Volume(handle)
            name = volume.name
            files = [e for _p, e in volume.walk() if e.is_file]
            total = sum(e.size for e in files)
        return DiskMatch(path, name, role_for(name), len(files), total,
                         version=version_of(name, path.name))
    except (amigafs.AmigaFsError, OSError, ValueError) as error:
        return DiskMatch(path, "", None, 0, 0, str(error))


def scan(folder: str | Path, progress: Progress | None = None) -> list[DiskMatch]:
    """Identify every ADF under ``folder``."""
    folder = Path(folder)
    found: list[DiskMatch] = []
    candidates = sorted(folder.rglob("*.adf")) + sorted(folder.rglob("*.ADF"))
    for index, candidate in enumerate(sorted(set(candidates)), start=1):
        match = identify(candidate)
        if match.role is not None:
            found.append(match)
        if progress:
            progress.fraction(index / max(1, len(candidates)))
    return found


def available_versions(matches: list[DiskMatch]) -> list[str]:
    """Releases for which a Workbench disk is present, newest first."""
    versions = {m.version for m in matches
                if m.role and m.role.key == "workbench" and m.version
                #  Guard against a stray revision number being read as a release.
                and m.version.split(".")[0].isdigit()
                and 1 <= int(m.version.split(".")[0]) <= 4}
    return sorted(versions, key=lambda v: [int(p) for p in v.split(".")],
                  reverse=True)


def choose_set(matches: list[DiskMatch], version: str = "") -> dict[str, DiskMatch]:
    """Pick the best disk for each role, keeping the whole set one release.

    A collection usually holds many dumps of each disk - verified, modified,
    alternate, beta - and often several AmigaOS releases side by side.  Mixing
    releases produces a broken install (a 2.0 Extras drawer on a 3.1 system, for
    example), so the release is decided first, from the Workbench disk, and
    every other disk is then matched to it.
    """
    if not version:
        candidates = [m for m in matches if m.role and m.role.key == "workbench"]
        if candidates:
            best = max(candidates, key=lambda m: (m.quality, m.file_count))
            version = best.version
        version = version or (available_versions(matches)[:1] or [""])[0]

    def score(match: DiskMatch) -> tuple:
        #  A matching release outranks dump quality; an unversioned disk (Fonts,
        #  Locale) is acceptable but never beats an exact match.
        return (2 if match.version == version else (1 if not match.version else 0),
                match.quality, match.file_count)

    best: dict[str, DiskMatch] = {}
    for match in matches:
        if match.role is None:
            continue
        if match.version and version and match.version != version:
            continue
        current = best.get(match.role.key)
        if current is None or score(match) > score(current):
            best[match.role.key] = match
    return best


def missing_roles(chosen: dict[str, DiskMatch]) -> list[Role]:
    return [r for r in ROLES if r.required and r.key not in chosen]


def estimate_size(chosen: dict[str, DiskMatch]) -> int:
    """Roughly how much space the install needs, with FFS overhead."""
    payload = sum(m.total_bytes for m in chosen.values())
    blocks = sum(m.file_count for m in chosen.values())
    #  One header block per file, plus slack for directories and the bitmap.
    return int(payload * 1.05) + blocks * amigafs.BLOCK + 2 * 1024 * 1024


def make_volume(handle, offset: int, total_blocks: int, name: str, dostype: int):
    """Format a partition, choosing the file system implementation by DosType.

    PFS3 and FFS writers present the same interface, so everything that fills a
    volume works against either.
    """
    if dostype in (rdb.DOSTYPE_PFS3, rdb.DOSTYPE_PDS3):
        writer = pfs3.Pfs3Writer(handle, offset, total_blocks, name)
        writer.format()
        return writer
    return VolumeWriter.format(handle, offset, total_blocks, name, dostype=dostype)


def open_amiga_volume(path: str | Path, partition: str = ""):
    """Open any Amiga volume for reading, whatever shape it arrives in.

    Accepts a bare file system image, an ``.hdf`` with a Rigid Disk Block, or a
    whole card image; for the latter two a partition is chosen (the bootable
    one, or one named by ``partition``).  Returns ``(reader, description)``.
    """
    from . import builder as builder_module

    path = Path(path)
    handle = open(path, "rb")
    located = builder_module.find_rdb(handle)
    if located is None:
        #  A bare file system with no partition table in front of it.
        handle.seek(0)
        signature = handle.read(4)
        if signature[:3] == b"DOS":
            return Volume(handle), f"{path.name} (bare FFS/OFS volume)"
        if signature == b"PFS\x01":
            return pfs3.Pfs3Volume(handle, 0), f"{path.name} (bare PFS3 volume)"
        handle.close()
        raise RuntimeError(f"{path.name} holds no Amiga file system we can read")

    base, table = located
    candidates = table.partitions
    if partition:
        candidates = [p for p in candidates
                      if p.drive_name.upper() == partition.upper()] or candidates
    chosen = next((p for p in candidates if p.bootable), None) or candidates[0]
    offset = chosen.byte_offset(table.geometry, base)
    blocks = chosen.blocks(table.geometry)
    label = f"{path.name} partition {chosen.drive_name} " \
            f"({rdb.dostype_name(chosen.dostype)})"
    if chosen.dostype in (rdb.DOSTYPE_PFS3, rdb.DOSTYPE_PDS3):
        return pfs3.Pfs3Volume(handle, offset), label
    return Volume(handle, offset, blocks), label


def _excluded(lowered: str, skip: list[str]) -> bool:
    """Whether ``lowered`` is inside one of the excluded paths.

    A drawer's icon is its *sibling*, not its child: excluding
    ``WHDLOAD/AGA`` has to take ``WHDLOAD/AGA.info`` with it, or Workbench
    draws an icon for a drawer that is not on the card and opening it fails.
    Matching only the drawer and its contents left exactly that behind.
    """
    for entry in skip:
        if lowered == entry or lowered.startswith(entry + "/"):
            return True
        if lowered == entry + ".info":
            return True
    return False


def landed_path(destination: str, relative: str) -> str:
    """Where a file being copied will live on the card.

    Every compatibility rule is written about the card - Storage/Monitors/PAL,
    Libs/Picasso96 - while a copy knows only where a file sits in the thing
    being copied from. Asking about the source path meant no rule naming a
    destination drawer could ever match.
    """
    return f"{destination}/{relative}".lstrip("/") if destination else relative


def copy_volume(source, target, destination: str, progress: Progress,
                 skip_existing: bool = True, compat=None,
                 exclude: list[str] | None = None) -> tuple[int, int]:
    """Copy an entire source volume into ``destination`` on the target.

    Works for FFS and PFS3 alike: both readers walk the same way and their
    entries expose the same fields.  Copying the *contents* rather than the
    blocks is what lets an imported system be adapted on the way in - a block
    copy would carry the emulator's graphics driver across untouched.
    """
    skip = [e.replace("\\", "/").strip("/").lower() for e in (exclude or [])]
    base = target.makedirs(destination) if destination else target.root
    dir_blocks: dict[str, int] = {"": base}
    copied = skipped = 0
    entries = list(source.walk())
    for index, (path, entry) in enumerate(entries, start=1):
        progress.check_cancelled()
        lowered = path.lower()
        if _excluded(lowered, skip):
            skipped += 1
            continue
        parent_path, _, name = path.rpartition("/")
        parent = dir_blocks.get(parent_path)
        if parent is None:
            continue                    # its directory was skipped
        if entry.is_dir:
            dir_blocks[path] = target.mkdir(
                parent, name, protect=entry.protect, comment=entry.comment,
                days=entry.days, mins=entry.mins, ticks=entry.ticks)
        else:
            if skip_existing and target._entry_exists(parent, name) is not None:
                skipped += 1
                continue
            data = source.read_file(entry)
            if compat is not None:
                landed = landed_path(destination, path)
                data = compat.offer(landed, data)
                if compat.skip(landed):
                    skipped += 1
                    continue
                #  A distribution can ship its finished boot script under
                #  another name, to be renamed once its installer has run.
                instead = getattr(compat, "rename_to", None)
                if instead is not None:
                    name = instead(landed) or name
            target.write_file(parent, name, data, protect=entry.protect,
                              comment=entry.comment, days=entry.days,
                              mins=entry.mins, ticks=entry.ticks)
            copied += 1
        if index % 200 == 0 or index == len(entries):
            progress.fraction(index / len(entries))
    if compat is not None and getattr(compat, "finish_with_each_tree", True):
        #  Without this the emulator's driver is removed and nothing put back.
        compat.finish(target, progress)
    return copied, skipped


#  AmigaDOS file names: at most 30 characters, and these are reserved.
ILLEGAL_AMIGA_CHARS = set(':/')


#  An icon is always "<name>.info", and AmigaDOS allows 30 characters, so a
#  file that has an icon can only use 25 of them.  Truncating the two
#  independently is what orphans an icon from its file - and a WHDLoad game is
#  launched from the tool types in its icon, so an orphaned icon is a game that
#  no longer starts.
ICON_SUFFIX = ".info"


def _fold(char: str) -> str:
    """One character in a form ISO-8859-1 - the Amiga's character set - holds."""
    try:
        char.encode("latin-1")
        return char
    except UnicodeEncodeError:
        pass
    stripped = "".join(c for c in unicodedata.normalize("NFKD", char)
                       if not unicodedata.combining(c))
    try:
        stripped.encode("latin-1")
    except UnicodeEncodeError:
        return "_"
    return stripped or "_"


def _host_text(name: str) -> str:
    """What a host file's name says, whichever encoding it was stored in.

    Linux keeps file names as bytes.  Python decodes them as UTF-8 and hands
    back surrogate escapes for the bytes that are not, so a name has to be read
    from the bytes to be read at all.
    """
    raw = os.fsencode(name)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        #  Not UTF-8, so these bytes are already an Amiga (ISO-8859-1) name.
        return raw.decode("latin-1")


def _amiga_spelling(name: str) -> str:
    """How a host file's name should be spelt on the Amiga.

    A tree that came off an Amiga carries ISO-8859-1 names - "fran\xe7ais",
    "portugu\xeas", "\xf6sterreich.country" - and Linux stores those bytes
    exactly as they are.  Python cannot decode them as UTF-8, so it hands them
    back as surrogate escapes; spelling such a name for the Amiga means giving
    the original bytes back, not replacing the byte Python could not read.
    Writing "portugu?s.language" instead leaves a locale AmigaOS will never
    find again - and "?" is a pattern wildcard to AmigaDOS at that.

    A name that really is UTF-8 text may still use characters ISO-8859-1 has no
    room for.  Those are folded to their unaccented form ("\u010de\u0161tina"
    -> "cestina"), which at least remains a name a person can type.
    """
    text = _host_text(name)
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        text = "".join(_fold(c) for c in text)
    return text


def _clean(name: str) -> str:
    return "".join("_" if c in ILLEGAL_AMIGA_CHARS or ord(c) < 32 else c
                   for c in _amiga_spelling(name))


def _fit(name: str, limit: int, taken: set[str]) -> tuple[str, str]:
    """Shorten ``name`` to ``limit`` characters, keeping it unique.

    Returns the name to use and why it differs from the one asked for, so that
    the log can say what actually happened: ``""`` when nothing changed,
    ``"charset"`` when a character had to be rewritten, ``"shortened"`` when the
    name did not fit, and ``"clash"`` when another entry already had it.
    """
    cleaned = _clean(name)
    #  Compare against what the host name *says*, not the Python string holding
    #  it.  A name Linux could not decode is a string of surrogate escapes that
    #  never equals the name it stands for, and one stored as UTF-8 is a
    #  different string from the same name stored as ISO-8859-1 - reporting
    #  either as rewritten would claim a rename where the Amiga sees none.
    reason = "" if cleaned == _host_text(name) else "charset"
    if len(cleaned) > limit:
        reason = "shortened"
        #  Only a truncated name needs tidying.  A trailing dot is perfectly
        #  legal on AmigaDOS - only ":" and "/" are reserved - so "MOD.doober."
        #  must be left exactly as it is rather than quietly renamed.
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 6 and len(ext) + 1 < limit:
            stem = stem[:limit - len(ext) - 1].rstrip(".")
            cleaned = f"{stem}.{ext}" if stem else ext[:limit]
        else:
            cleaned = cleaned[:limit].rstrip(".")
        cleaned = cleaned or "_"

    if cleaned.lower() not in taken:
        return cleaned, reason
    #  Make room for a counter rather than silently colliding: two entries with
    #  the same name in one directory means only the first is ever found.
    for index in range(2, 1000):
        suffix = f"_{index}"
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 6:
            base = stem[:max(1, limit - len(ext) - 1 - len(suffix))].rstrip(".")
            candidate = f"{base}{suffix}.{ext}"
        else:
            candidate = cleaned[:max(1, limit - len(suffix))] + suffix
        if candidate.lower() not in taken:
            return candidate, reason or "clash"
    raise RuntimeError(f"cannot find a free name for {name!r}")


def name_limit(target) -> int:
    """How long a file name the target volume will accept."""
    return getattr(target, "max_name_length", amigafs.MAX_NAME)


def plan_names(names: list[str], limit: int = amigafs.MAX_NAME,
               preferred: frozenset[str] = frozenset()) -> dict[str, str]:
    """Choose Amiga names for one directory's entries, keeping icons paired.

    See ``_plan_names``, of which this is the answer without the reasons.
    """
    return {name: chosen for name, (chosen, _why) in
            _plan_names(names, limit, preferred).items()}


def _plan_names(names: list[str], limit: int = amigafs.MAX_NAME,
                preferred: frozenset[str] = frozenset()
                ) -> dict[str, tuple[str, str]]:
    """Choose Amiga names for one directory's entries, keeping icons paired.

    Names are decided for a whole directory at once because the choices are not
    independent: an icon has to end up named after whatever its file was called,
    and two entries must not be shortened onto the same name.

    Matching is case-insensitive, as AmigaDOS is.  A collection copied off a
    case-sensitive host is full of icons whose case differs from their file -
    "Eagleplayer" alongside "EaglePlayer.info" - and treating those as two
    different names invents a clash that does not exist and renames a file that
    was perfectly good.

    ``limit`` comes from the target volume.  FFS allows 30 characters; PFS3
    reads its own limit from the volume and can take far more, which matters
    because renaming a file breaks the WHDLoad slave or tool type that names it.

    Names are settled in a fixed order so that the answer does not depend on
    what order the file system listed them in, and ``preferred`` goes to the
    front of it.  Where two entries cannot both keep their name, the first one
    considered is the one that keeps it, so this is how the caller says which
    of them matters.
    """
    #  A file called exactly ".info" is an ordinary name, not an icon.
    icons = [n for n in names
             if n.lower().endswith(ICON_SUFFIX) and n.lower() != ICON_SUFFIX]
    plain = [n for n in names if n not in set(icons)]

    by_lower = {n.lower(): n for n in plain}
    owner_of = {icon: by_lower.get(icon[:-len(ICON_SUFFIX)].lower(),
                                   icon[:-len(ICON_SUFFIX)])
                for icon in icons}
    owners_with_icons = set(owner_of.values())
    orphans = [o for o in owners_with_icons if o.lower() not in by_lower]

    mapping: dict[str, tuple[str, str]] = {}
    taken: set[str] = set()
    #  Decide the files first; the icons then follow their files.
    for original in sorted(set(plain) | set(orphans),
                           key=lambda name: (name not in preferred, name)):
        room = limit - len(ICON_SUFFIX) if original in owners_with_icons else limit
        chosen, reason = _fit(original, max(1, room), taken)
        taken.add(chosen.lower())
        mapping[original] = (chosen, reason)

    for icon, owner in owner_of.items():
        if mapping.get(owner, ("", ""))[0] == owner and len(icon) <= limit:
            #  Nothing had to change, so leave the icon's own spelling alone.
            mapping[icon] = (icon, "")
        else:
            chosen = mapping[owner][0] + ICON_SUFFIX
            mapping[icon] = (chosen, mapping[owner][1] if chosen != icon else "")
        taken.add(mapping[icon][0].lower())

    #  An icon's base name is worked out even when no such file is present;
    #  return a mapping for exactly the entries that were asked about.
    return {name: mapping[name] for name in names}


def _read_source(path: Path, relative: str, compat, progress: Progress,
                 attempts: int = 3) -> bytes:
    """Read a source file, retrying and substituting rather than skipping.

    An unreadable file must never be passed over silently: a system missing one
    library boots to a broken desktop, and the log would have claimed success.
    """
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            return path.read_bytes()
        except OSError as error:
            last = error
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))
    spare = compat.spare_for(relative) if compat is not None else None
    if spare is not None:
        try:
            data = spare.read_bytes()
        except OSError:
            data = None
        if data is not None:
            progress.log(f"  {_printable(relative)} could not be read "
                         f"({last}); using the known-good copy from {spare}")
            return data
    raise RuntimeError(
        f"Could not read {_printable(relative)} from the source ({last}). The "
        f"copy would be incomplete, so it has been stopped. Check the source "
        f"is fully readable, or supply a replacement copy of the file."
    )


@dataclasses.dataclass(frozen=True)
class _Placement:
    """Where one host entry ends up on the Amiga, and why it moved."""
    name: str                 # its Amiga name; "" when it is left out
    parent: str               # Amiga path of the drawer holding it
    path: str                 # Amiga path of the entry itself
    reason: str = ""          # "", "charset", "shortened", "clash",
                              # "merged", "duplicate" or "unreachable"
    instead: str = ""         # the entry that took its name, when left out


def _printable(path: str) -> str:
    """A host path in a form that can be read, and written to a log.

    Names Linux could not decode arrive as surrogate escapes, which no stream
    can encode and nobody can read: a log line about
    "Locale/Countries/espa\udcf1a.country" names no file the user recognises.
    Each part is shown as what it says, which is also what it will be called on
    the Amiga.  Parts are taken one at a time because a single tree can hold
    both spellings, and reading the whole path in either would garble the other.
    """
    return "/".join(_host_text(part) for part in path.split("/"))


def _names_used_by_icons(members: dict[str, tuple[Path, bool, str]]) -> set[str]:
    """Every file name the icons in one drawer refer to by name.

    A WHDLoad drawer's icon names its slave in a ``SLAVE=`` tool type, and an
    icon can name a default tool the same way.  That is a reference to one
    exact spelling, which matters when two spellings have to become one.
    """
    used: set[str] = set()
    for name, (path, is_dir, _relative) in members.items():
        if is_dir or not name.lower().endswith(ICON_SUFFIX):
            continue
        try:
            entries = amigainfo.read_tooltypes(path.read_bytes())
        except (OSError, amigainfo.InfoError):
            continue                      # an icon we cannot read names nothing
        for entry in entries:
            _key, _sep, value = entry.partition("=")
            if value:
                used.add(value)
    return used


def _keep_first(group: list[str], used: set[str]) -> list[str]:
    """Order a clash so that the spelling something refers to comes first.

    Two names that differ only in case are one name on the Amiga, so one of
    them has to give - and which one is not arbitrary.  A collection built for
    an emulator that mounts the host directory keeps both, and the emulator
    opens whichever the icon names exactly; keeping the other would launch a
    different build of the game than the same collection runs under the
    emulator it came from.  Where the references are ambiguous, or name
    neither spelling, the order it was already in stands.
    """
    named = [name for name in group if name in used]
    if len(named) != 1:
        return group
    return named + [name for name in group if name != named[0]]


#  Reasons a name is genuinely different from the one that was asked for.  A
#  name that changed only in case is not among them: AmigaDOS finds it anyway.
RENAMED_REASONS = ("shortened", "charset", "clash")


def _repoint_icon(data: bytes,
                  renames: dict[str, str]) -> tuple[bytes, list[tuple[str, str]]]:
    """Point an icon's tool types at files whose names had to change.

    Tool types are how an icon names a file - a WHDLoad game's ``SLAVE=`` above
    all - so shortening a file and leaving its icon alone is exactly the case
    the length warning is about.  Where the name changed for a reason AmigaDOS
    cannot see through, the reference is rewritten to match.

    Only a value that is the whole name of an entry in the same drawer is
    touched.  A value naming a path leads somewhere this drawer's renames say
    nothing about, and rewriting it on a matching last component would be a
    guess.
    """
    if not renames:
        return data, []
    try:
        entries = amigainfo.read_tooltypes(data)
    except amigainfo.InfoError:
        return data, []                   # not an icon we can read; leave it be

    changed: list[tuple[str, str]] = []
    out: list[str] = []
    for entry in entries:
        #  A tool type in parentheses is one Workbench shows but ignores.
        opener, closer, body = "", "", entry
        if body.startswith("(") and body.endswith(")"):
            opener, closer, body = "(", ")", body[1:-1]
        key, sep, value = body.partition("=")
        chosen = renames.get(_clean(value).lower()) if sep and value else None
        if chosen is None or chosen == value:
            out.append(entry)
            continue
        out.append(f"{opener}{key}={chosen}{closer}")
        changed.append((value, chosen))
    if not changed:
        return data, []
    return amigainfo.write_tooltypes(data, out), changed


def _encoding_of(name: str) -> str:
    """Which encoding the host used to store this name."""
    try:
        os.fsencode(name).decode("utf-8")
    except UnicodeDecodeError:
        return "ISO-8859-1"
    return "UTF-8"


def _describe_left_out(relative: str, kept: str) -> str:
    """Say why a file could not be copied, in terms of the names involved.

    Usually the two names differ in case.  Sometimes they are the *same* name
    stored two ways - "espa\xf1a.country" as an Amiga writes it, and the same
    text as UTF-8 - and then both print identically, so reporting that one
    cannot be told from the other reads as nonsense unless the encodings are
    named.  PiMiga's Locale drawer has several of these.
    """
    shown, kept_shown = _printable(relative), _printable(kept)
    if shown.rpartition("/")[2] != kept_shown:
        return (f"{shown} left out: AmigaDOS cannot tell it from {kept_shown}, "
                f"which is the copy that is kept")
    keeps = _encoding_of(kept)
    return (f"{shown} left out: the source stores this name twice, as "
            f"{_encoding_of(relative.rpartition('/')[2])} and as {keeps}; "
            f"AmigaDOS has one name for both, so the {keeps} copy is kept")


def _identical(first: Path, second: Path) -> bool:
    """Whether two host files hold exactly the same bytes."""
    try:
        return filecmp.cmp(first, second, shallow=False)
    except OSError:
        return False


def _place_entries(entries: list[tuple[Path, str, bool]],
                   limit: int) -> dict[str, _Placement]:
    """Decide where every entry of a host tree lands on the Amiga.

    Names cannot be decided one at a time.  An icon has to keep the name of the
    file it belongs to, two entries must not land on the same name, and - the
    reason this walks the tree rather than each directory alone - AmigaDOS is
    case-insensitive, so entries a Linux tree keeps apart only by case are one
    and the same thing here.

    Collections assembled on Linux are full of those: "Bombuzal.slave" beside
    an identical "Bombuzal.Slave", "data" beside "Data".  Only one of each pair
    can exist here, and - this is the part that decides what to do with the
    other - only one of them can be *reached*: every spelling of a name finds
    the same entry, so a second copy kept under an invented name like
    "Bombuzal_2.slave" is a file nothing will ever open.  It is left out
    instead, whether or not it holds the same bytes.  Two drawers of the same
    name are merged into one rather than one of them renamed, which would leave
    a game looking for half of its files.  A name is only really renamed where
    a file clashes with a drawer, which cannot be resolved either way.
    """
    by_parent: dict[str, list[tuple[Path, str, bool]]] = {}
    for path, relative, is_dir in entries:
        parent, _, name = relative.rpartition("/")
        by_parent.setdefault(parent, []).append((path, name, is_dir))

    placements: dict[str, _Placement] = {}
    #  Each item is one Amiga drawer and the host directories feeding it - more
    #  than one where their names differed only in case.
    queue: list[tuple[str, list[str]]] = [("", [""])]
    while queue:
        amiga_parent, hosts = queue.pop(0)
        members: dict[str, tuple[Path, bool, str]] = {}
        for host in hosts:
            for path, name, is_dir in by_parent.get(host, []):
                members[name] = (path, is_dir,
                                 f"{host}/{name}" if host else name)

        same_name: dict[str, list[str]] = {}
        for name in sorted(members):
            same_name.setdefault(_clean(name).lower(), []).append(name)

        #  Only a drawer that has a clash to settle needs its icons read.
        used = (_names_used_by_icons(members)
                if any(len(g) > 1 for g in same_name.values()) else set())

        survivors: list[str] = []
        preferred: set[str] = set()       # the spelling that keeps its name
        joins: dict[str, str] = {}        # merged drawer -> the one it joins
        duplicates: set[str] = set()      # an identical copy of another
        unreachable: dict[str, str] = {}  # differs, but shares another's name
        for group in same_name.values():
            if len(group) > 1:
                group = _keep_first(group, used)
                preferred.add(group[0])
            keep = group[0]
            survivors.append(keep)
            keep_path, keep_is_dir, _ = members[keep]
            for other in group[1:]:
                other_path, other_is_dir, _ = members[other]
                if keep_is_dir and other_is_dir:
                    joins[other] = keep
                elif not keep_is_dir and not other_is_dir:
                    #  Same bytes or not, nothing on the Amiga could open it.
                    if _identical(keep_path, other_path):
                        duplicates.add(other)
                    else:
                        unreachable[other] = keep
                else:
                    #  A file and a drawer of the same name: neither can give
                    #  way to the other, so one has to be renamed after all.
                    survivors.append(other)

        chosen = _plan_names(sorted(survivors), limit, frozenset(preferred))
        children: dict[str, list[str]] = {}
        for name in sorted(members):
            _path, is_dir, relative = members[name]
            if name in duplicates:
                placements[relative] = _Placement("", amiga_parent, "",
                                                  "duplicate")
                continue
            if name in unreachable:
                placements[relative] = _Placement("", amiga_parent, "",
                                                  "unreachable",
                                                  unreachable[name])
                continue
            amiga_name, reason = chosen[joins.get(name, name)]
            if name in joins:
                reason = "merged"
            amiga_path = (f"{amiga_parent}/{amiga_name}" if amiga_parent
                          else amiga_name)
            placements[relative] = _Placement(amiga_name, amiga_parent,
                                              amiga_path, reason)
            if is_dir:
                children.setdefault(amiga_path, []).append(relative)
        for amiga_path in sorted(children):
            queue.append((amiga_path, children[amiga_path]))
    return placements


def install_tree(target: VolumeWriter, source: str | Path, destination: str,
                 progress: Progress,
                 compat: "compat_module.Compatibility | None" = None,
                 exclude: list[str] | None = None,
                 merge: bool = False,
                 written: list[str] | None = None) -> tuple[int, int]:
    """Copy a host directory tree into an Amiga volume.

    This is how a directory-based drive from an emulator - PiMiga's
    ``disks/System`` and friends, which Amiberry mounts straight off the Linux
    file system - becomes a real Amiga partition that AmigaOS can boot from on
    bare metal.

    ``written``, if given, is filled in with the path of every file this call
    really wrote, under the name it was written as.  Not the same list as the
    source tree: a file the volume already had, one a compatibility rule
    refused, and one that could not be written are all absent from it.  What
    was asked for and what arrived are different things, and only the second
    is worth recording.

    ``merge`` is for a tree laid on top of a volume that already has files in
    it - an overlay.  Without it a drawer the volume already has is created a
    second time rather than added to, and AmigaDOS only ever finds the first
    of the two: everything the overlay put in its own ``Libs`` or ``S`` is
    invisible.  It costs a directory scan per drawer, so filling a freshly
    formatted volume leaves it off.
    """
    source = Path(source)
    if not source.is_dir():
        raise RuntimeError(f"{source} is not a directory")

    skip = [e.replace("\\", "/").strip("/").lower() for e in (exclude or [])]
    entries: list[tuple[Path, str, bool]] = []
    skipped_paths = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            continue
        relative = str(path.relative_to(source)).replace(os.sep, "/")
        lowered = relative.lower()
        if _excluded(lowered, skip):
            skipped_paths += 1
            continue
        entries.append((path, relative, path.is_dir()))
    if skipped_paths:
        progress.log(f"  left out {skipped_paths} item(s) not suited to this "
                     f"machine")

    #  Decide names for the whole tree first, so an icon keeps the name of the
    #  file it belongs to and nothing collides.
    limit = name_limit(target)
    placements = _place_entries(entries, limit)

    #  What each drawer's icons may still be naming.  Keyed on the cleaned,
    #  lower-cased old name, because AmigaDOS matches without regard to case
    #  and a tool type may not spell it as the file system did.
    renames: dict[str, dict[str, str]] = {}
    for relative, placed in placements.items():
        if placed.reason in RENAMED_REASONS:
            old = relative.rpartition("/")[2]
            renames.setdefault(placed.parent, {})[_clean(old).lower()] = placed.name

    base = target.makedirs(destination) if destination else target.root
    dir_blocks: dict[str, int] = {"": base}
    copied = 0
    changes: dict[str, int] = {}

    for index, (path, relative, is_dir) in enumerate(entries, start=1):
        progress.check_cancelled()
        placed = placements.get(relative)
        parent = None if placed is None else dir_blocks.get(placed.parent)
        if parent is None:
            continue                      # its drawer was skipped, so is it
        if placed.reason:
            changes[placed.reason] = changes.get(placed.reason, 0) + 1
        if placed.reason == "duplicate":
            continue
        if placed.reason == "unreachable":
            #  Worth naming one by one: unlike an identical copy, this file
            #  held something of its own, and the card will not have it.
            progress.log("  " + _describe_left_out(relative, placed.instead))
            continue
        if placed.reason in ("charset", "shortened", "clash"):
            progress.log(f"  {_printable(relative)} -> {_printable(placed.name)}")
        try:
            if is_dir:
                #  A drawer merged into one already made keeps that one; other
                #  than that nothing can exist on a freshly formatted volume,
                #  and checking would mean walking the directory per entry.
                block = dir_blocks.get(placed.path)
                if block is None:
                    block = target.mkdir(parent, placed.name,
                                         check_existing=merge)
                dir_blocks[placed.path] = block
            else:
                data = _read_source(path, relative, compat, progress)
                if compat is not None:
                    landed = landed_path(destination, relative)
                    data = compat.offer(landed, data)
                    if compat.skip(landed):
                        continue
                if placed.name.lower().endswith(ICON_SUFFIX):
                    data, repointed = _repoint_icon(
                        data, renames.get(placed.parent, {}))
                    for was, now in repointed:
                        changes["repointed"] = changes.get("repointed", 0) + 1
                        progress.log(f"  {_printable(relative)}: {was} -> {now} "
                                     f"(the file it names had to be renamed)")
                target.write_file(parent, placed.name, data,
                                  check_existing=merge)
                copied += 1
                if written is not None:
                    written.append(landed_path(destination, placed.path))
        except (amigafs.AmigaFsError, pfs3.Pfs3Error) as error:
            progress.log(f"  skipped {_printable(relative)}: {error}")
        if index % 200 == 0 or index == len(entries):
            progress.fraction(index / len(entries))

    shortened = changes.get("shortened", 0)
    renamed = shortened + changes.get("clash", 0) + changes.get("charset", 0)
    if shortened:
        #  Only worth suggesting where there is somewhere to go: PFS3 already
        #  gives far more than FFS's thirty, and telling someone to switch to
        #  the file system they are already using is no help at all.
        advice = (" A PFS3 partition avoids this."
                  if limit <= amigafs.MAX_NAME else
                  " These names are simply longer than any Amiga file system "
                  "will hold.")
        progress.log(f"WARNING: {shortened} name(s) had to be shortened to fit "
                     f"{limit} characters. Software that refers to a file by "
                     f"name - a WHDLoad slave, an icon's tool types - may no "
                     f"longer find it.{advice}")
    if changes.get("clash"):
        progress.log(f"  {changes['clash']} name(s) differed from another in "
                     f"the same drawer only in case but held different "
                     f"contents, so one of each was renamed - AmigaDOS cannot "
                     f"tell such names apart")
    if changes.get("charset"):
        progress.log(f"  {changes['charset']} name(s) used characters AmigaOS "
                     f"cannot store and were rewritten")
    if changes.get("duplicate"):
        progress.log(f"  {changes['duplicate']} file(s) differing from another "
                     f"only in case, with identical contents, were left out")
    if changes.get("unreachable"):
        progress.log(f"  {changes['unreachable']} file(s) differed from "
                     f"another only in case but held different contents; "
                     f"AmigaDOS can reach only one of each, so the copy "
                     f"nothing refers to was left out")
    if changes.get("repointed"):
        progress.log(f"  {changes['repointed']} icon reference(s) were "
                     f"rewritten to match a file that had to be renamed")
    if changes.get("merged"):
        progress.log(f"  {changes['merged']} drawer(s) differing from another "
                     f"only in case were merged into one")
    if compat is not None and getattr(compat, "finish_with_each_tree", True):
        compat.finish(target, progress)
    return copied, renamed


def tree_size(source: str | Path) -> tuple[int, int]:
    """Total bytes and file count of a host directory tree."""
    total = count = 0
    for path in Path(source).rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
                count += 1
            except OSError:
                pass
    return total, count


def install(handle, offset: int, total_blocks: int, chosen: dict[str, DiskMatch],
            progress: Progress, *, volume_name: str = "Workbench",
            dostype: int = amigafs.DOSTYPE_FFS_INTL,
            close: bool = True, edit=None) -> VolumeWriter:
    """Format a partition and copy the chosen Workbench disks into it."""
    missing = missing_roles(chosen)
    if missing:
        raise RuntimeError(
            "Cannot install AmigaOS: missing the "
            + ", ".join(r.label for r in missing) + " disk."
        )

    progress.step(f"Creating the {volume_name} file system")
    target = make_volume(handle, offset, total_blocks, volume_name, dostype)
    progress.log(f"Formatted {human_size(total_blocks * amigafs.BLOCK)} as a "
                 f'{rdb.dostype_name(dostype)} volume named "{volume_name}"')

    ordered = sorted(chosen.values(), key=lambda m: m.role.order)
    total_copied = 0
    for match in ordered:
        progress.check_cancelled()
        where = match.role.destination or volume_name + ":"
        progress.step(f"Installing {match.role.label} into {where}")
        progress.log(f'{match.path.name}  (volume "{match.volume_name}")')
        with open(match.path, "rb") as source_handle:
            source = Volume(source_handle)
            copied, skipped = copy_volume(source, target,
                                           match.role.destination, progress,
                                           compat=edit)
        total_copied += copied
        progress.log(f"  {copied} files copied"
                     + (f", {skipped} already present" if skipped else ""))

    #  A few drawers the install script creates that no disk provides.
    for extra in ("T", "Trashcan", "Devs/DOSDrivers", "Prefs/Env-Archive"):
        target.makedirs(extra)

    if close:
        progress.step("Finalising the Amiga file system")
        target.close()
    progress.log(f"{total_copied} files installed; "
                 f"{human_size(target.free_bytes)} free on {volume_name}:")
    return target


#  Drawers Workbench 3.1 deliberately leaves without an icon.  These are the
#  system's working parts, not places a person browses to, and giving them
#  icons would clutter the desktop with drawers Commodore chose to hide.
HIDDEN_DRAWERS = {
    "c", "l", "s", "libs", "devs", "fonts", "locale", "classes", "t",
    "rexxc", "expansion", "prefs/env-archive",
}


def _is_hidden(path: str) -> bool:
    """Whether Workbench would never show this drawer anyway.

    A drawer inside a hidden one is hidden too: an icon on ``Classes/Gadgets``
    can only be seen by someone who has already turned on Show All Files to
    get into ``Classes`` at all, so writing one is just a file nobody sees.
    """
    lowered = path.lower()
    parts = lowered.split("/")
    for depth in range(1, len(parts) + 1):
        if "/".join(parts[:depth]) in HIDDEN_DRAWERS:
            return True
    return False


def _drawer_icon_sources(folders: Iterable[str | Path]) -> dict[str, bytes]:
    """Every drawer icon found in ``folders``, keyed on the drawer's name.

    MagicWB keeps its drawer icons beside a one-byte stub of the drawer they
    are for, so the ``.info`` files alone are what is wanted.  A donor system
    simply has them next to the real drawers.
    """
    found: dict[str, bytes] = {}
    for folder in folders:
        base = Path(folder)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.info")):
            stem = path.name[:-5]
            if not stem:
                continue
            data = path.read_bytes()
            #  Only a drawer icon opens a drawer.  Matching purely on the name
            #  handed the Storage/Install drawer MagicWB's Install.info, which
            #  is the project icon for MagicWB's own installer script, and a
            #  double click answered "unable to open script".
            if not amigainfo.is_drawer_icon(data):
                continue
            #  First one wins, so the order folders are passed in is the
            #  order of preference.
            found.setdefault(stem.lower(), data)
    return found


def drawer_icons_from_volume(reader, into: Path, limit: int = 40) -> int:
    """Copy an Amiga volume's own drawer icons out, to be copied from.

    A drive being imported brings a desktop that was designed: ClassicWB's
    drawers are MagicWB-styled, and the drawers this tool adds beside them -
    ``Internet/NetSurf``, ``Utilities/SysInfo`` - were given a stock
    Workbench 3.1 drawer instead, because the only icons offered came off the
    floppies. The result is a desktop where the software the user chose is
    the part that looks foreign.

    The drive's own icons are the right thing to copy, so they are taken from
    it and offered first. Only real drawer icons, and only from the root,
    which is where a distribution's own style is set.
    """
    into.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        entries = reader.listdir()
    except Exception:                                       # noqa: BLE001
        return 0
    names = {entry.name.lower() for entry in entries}
    for entry in entries:
        if written >= limit or not entry.name.lower().endswith(".info"):
            continue
        stem = entry.name[:-5]
        #  An icon whose drawer is not there belongs to something else.
        if stem.lower() not in names:
            continue
        try:
            data = reader.read_file(entry)
        except Exception:                                   # noqa: BLE001
            continue
        if not amigainfo.is_drawer_icon(data):
            continue
        (into / entry.name).write_bytes(data)
        written += 1
    return written


def drawer_icon_from_disks(folder: str | Path, into: Path) -> Path | None:
    """Take one real drawer icon out of the Workbench floppies.

    A card can now be built from floppies and Aminet alone, with no donor and
    no icon set - and then nothing had a drawer icon to copy, so the drawers
    this tool creates stayed invisible on Workbench. The floppies have real
    ones; one of those is as good as any.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return None
    for disk in sorted(folder.glob("*.adf")):
        try:
            volume, _label = open_amiga_volume(str(disk), "")
            entries = volume.listdir()
        except Exception:                        # noqa: BLE001 - try the next
            continue
        names = {e.name.lower() for e in entries}
        for entry in entries:
            name = entry.name
            if entry.is_dir or not name.lower().endswith(ICON_SUFFIX):
                continue
            if name[:-len(ICON_SUFFIX)].lower() not in names:
                continue
            try:
                data = volume.read_file(entry)
            except Exception:                    # noqa: BLE001
                continue
            if not amigainfo.is_drawer_icon(data):
                continue
            into.mkdir(parents=True, exist_ok=True)
            made = into / f"drawer{ICON_SUFFIX}"
            made.write_bytes(data)
            return made
    return None


def ensure_drawer_icons(volume, drawers: Iterable[str],
                        sources: Iterable[str | Path],
                        progress: Progress) -> int:
    """Give a drawer an icon when nothing else was going to.

    A drawer with no ``.info`` beside it does not appear on Workbench at all -
    it can only be reached from a Shell, or by turning on Show All Files.  That
    is correct for ``C:`` and ``LIBS:``, which is why Commodore ships them
    without icons, but this tool also creates drawers of its own to hold the
    software the user chose - ``Programs``, ``Internet``, ``AmiTCP`` - and gave
    them no icons either.  Every one of iGame, NetSurf, IBrowse and the rest
    was written to the card and then left unreachable from the desktop, which
    looks exactly like the software never having been installed.

    Icons are taken from real Amiga icons rather than invented: the chosen icon
    set or the donor system, matched on the drawer's own name where possible
    and otherwise any drawer icon among them, because one drawer icon is as
    good as another and having one is what matters.
    """
    icons = _drawer_icon_sources(sources)
    if not icons:
        return 0
    #  A stand-in for a drawer whose name nothing matched.  Preferring the
    #  plain Workbench drawers keeps it looking like a drawer.
    generic = next((icons[name] for name in ("drawer", "tools", "utilities",
                                             "storage", "system")
                    if name in icons), None)
    written = 0
    for drawer in drawers:
        path = drawer.strip("/")
        if not path or _is_hidden(path):
            continue
        parent_path, _, name = path.rpartition("/")
        try:
            parent = (volume.makedirs(parent_path) if parent_path
                      else volume.root)
        except Exception:
            continue
        if volume._entry_exists(parent, name) is None:
            continue                    # the drawer itself is not there
        if volume._entry_exists(parent, name + ".info") is not None:
            continue                    # it already has one
        data = icons.get(name.lower(), generic)
        if data is not None:
            #  Otherwise every drawer given the same fallback icon inherits
            #  that icon's snapshotted position and they all pile up.
            data = amigainfo.clear_position(data)
        if data is None:
            progress.log(f"  {path} has no icon and none was available; it "
                         f"will only show under Window/Show/All Files")
            continue
        volume.write_file(parent, name + ".info", data, check_existing=True)
        progress.log(f"  gave {path} an icon, so Workbench can show it")
        written += 1
    return written


class StartupSequenceEditor:
    """Insert lines into ``S:Startup-Sequence`` as it comes off the ADF.

    Some things have to be done before Workbench opens its first icon, and
    ``S:User-Startup`` is too late for them.  The clearest case is PeterK's
    ``icon.library``: a modern Amiga icon keeps its picture in an appended
    OS3.5 colour chunk and leaves the classic planar image empty, and the
    40.1 icon.library in Kickstart 3.1 cannot read that - it draws nothing at
    all, so a card full of perfectly good icons comes up with half of them
    blank.  The replacement on disk handles them, but soft-kicking it from
    ``S:User-Startup`` does nothing, because ``IPrefs`` has already opened the
    ROM one by then and a library in use cannot be flushed.  Asked which was
    live, the Amiga answered 40.1 while 51.4 sat unused in ``LIBS:``.

    The file cannot be rewritten after the fact - this file system creates
    files and never overwrites them - so it is edited in flight, on its way
    from the floppy image onto the card.

    It carries the same ``offer``/``skip`` pair the compatibility pass uses,
    so it can be handed to ``copy_volume`` in exactly the same way.
    """

    #  Insert above the first of these that appears.  IPrefs is the one that
    #  matters; SetPatch is the fallback for a Startup-Sequence that does not
    #  run IPrefs at all.
    ANCHORS = ("c:iprefs", "iprefs", "c:conclip")

    def __init__(self, lines: Iterable[str], progress: Progress):
        self.lines = [line for line in lines if line.strip()]
        self.progress = progress
        self.inserted = False
        self.replaced: list[str] = []

    def skip(self, relative: str) -> bool:
        """Nothing here is refused.

        This editor used to hold back files the floppies install so a donor's
        newer copy could take their place - Commodore's SetPatch, which only
        a donor could better. Everything is fetched from its publisher now,
        and an archive that means to replace a system file says so itself.
        """
        return False

    def finish(self, target, progress) -> None:
        """Nothing to do at the end; the whole edit happens in ``offer``.

        Part of the same trio ``copy_volume`` calls on the compatibility pass,
        so it has to be here even though it does nothing.
        """

    def offer(self, relative: str, data: bytes) -> bytes:
        posix = relative.replace("\\", "/").lower()
        if not self.lines or posix != "s/startup-sequence":
            return data
        text = data.decode("latin-1")
        out: list[str] = []
        done = False
        for line in text.splitlines(keepends=True):
            if not done and line.strip().lower().split()[:1] \
                    and line.strip().lower().split()[0] in self.ANCHORS:
                out.append("; Added by the PiStorm imager: this has to happen "
                           "before IPrefs opens\n")
                out.append("; the ROM icon.library, or the one on disk can "
                           "never replace it.\n")
                out += [entry + "\n" for entry in self.lines]
                done = True
            out.append(line)
        if not done:
            #  Nothing recognisable to sit in front of: put it after SetPatch,
            #  which is always the first line that matters.
            return data
        self.inserted = True
        self.progress.log(f"  S:Startup-Sequence: added {len(self.lines)} "
                          f"line(s) before IPrefs")
        return "".join(out).encode("latin-1")
