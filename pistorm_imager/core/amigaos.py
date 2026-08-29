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
import os
import re
import time
from pathlib import Path

from . import amigafs, compat as compat_module, pfs3, rdb
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
ROLES_BY_KEY = {r.key: r for r in ROLES}


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
    offset = base + chosen.start_block(table.geometry) * rdb.BLOCK
    blocks = chosen.blocks(table.geometry)
    label = f"{path.name} partition {chosen.drive_name} " \
            f"({rdb.dostype_name(chosen.dostype)})"
    if chosen.dostype in (rdb.DOSTYPE_PFS3, rdb.DOSTYPE_PDS3):
        return pfs3.Pfs3Volume(handle, offset), label
    return Volume(handle, offset, blocks), label


def _copy_volume(source, target, destination: str, progress: Progress,
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
        if any(lowered == e or lowered.startswith(e + "/") for e in skip):
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
                data = compat.offer(path, data)
                if compat.skip(path):
                    skipped += 1
                    continue
            target.write_file(parent, name, data, protect=entry.protect,
                              comment=entry.comment, days=entry.days,
                              mins=entry.mins, ticks=entry.ticks)
            copied += 1
        if index % 200 == 0 or index == len(entries):
            progress.fraction(index / len(entries))
    if compat is not None:
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
MAX_WITH_ICON = amigafs.MAX_NAME - len(ICON_SUFFIX)


def _clean(name: str) -> str:
    cleaned = "".join("_" if c in ILLEGAL_AMIGA_CHARS or ord(c) < 32 else c
                      for c in name)
    try:
        cleaned.encode("latin-1")
    except UnicodeEncodeError:
        cleaned = cleaned.encode("latin-1", errors="replace").decode("latin-1")
    return cleaned


def _fit(name: str, limit: int, taken: set[str]) -> str:
    """Shorten ``name`` to ``limit`` characters, keeping it unique."""
    cleaned = _clean(name)
    if len(cleaned) > limit:
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
        return cleaned
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
            return candidate
    raise RuntimeError(f"cannot find a free name for {name!r}")


def amiga_name(name: str, limit: int = amigafs.MAX_NAME) -> tuple[str, bool]:
    """Convert one host file name to a legal Amiga one."""
    fitted = _fit(name, limit, set())
    return fitted, fitted != name


def name_limit(target) -> int:
    """How long a file name the target volume will accept."""
    return getattr(target, "max_name_length", amigafs.MAX_NAME)


def plan_names(names: list[str], limit: int = amigafs.MAX_NAME) -> dict[str, str]:
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

    mapping: dict[str, str] = {}
    taken: set[str] = set()
    #  Decide the files first; the icons then follow their files.
    for original in sorted(set(plain) | set(orphans)):
        room = limit - len(ICON_SUFFIX) if original in owners_with_icons else limit
        chosen = _fit(original, max(1, room), taken)
        taken.add(chosen.lower())
        mapping[original] = chosen

    for icon, owner in owner_of.items():
        if mapping.get(owner) == owner and len(icon) <= limit:
            #  Nothing had to change, so leave the icon's own spelling alone.
            mapping[icon] = icon
        else:
            mapping[icon] = mapping[owner] + ICON_SUFFIX
        taken.add(mapping[icon].lower())

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
            progress.log(f"  {relative} could not be read ({last}); using the "
                         f"known-good copy from {spare}")
            return data
    raise RuntimeError(
        f"Could not read {relative} from the source ({last}). The copy would be "
        f"incomplete, so it has been stopped. Check the source is fully "
        f"readable, or supply a replacement copy of the file."
    )


def install_tree(target: VolumeWriter, source: str | Path, destination: str,
                 progress: Progress,
                 compat: "compat_module.Compatibility | None" = None,
                 exclude: list[str] | None = None) -> tuple[int, int]:
    """Copy a host directory tree into an Amiga volume.

    This is how a directory-based drive from an emulator - PiMiga's
    ``disks/System`` and friends, which Amiberry mounts straight off the Linux
    file system - becomes a real Amiga partition that AmigaOS can boot from on
    bare metal.
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
        if any(lowered == e or lowered.startswith(e + "/") for e in skip):
            skipped_paths += 1
            continue
        entries.append((path, relative, path.is_dir()))
    if skipped_paths:
        progress.log(f"  left out {skipped_paths} item(s) not suited to this "
                     f"machine")

    base = target.makedirs(destination) if destination else target.root
    dir_blocks: dict[str, int] = {"": base}
    copied = renamed = 0

    #  Decide names a directory at a time, so an icon keeps the name of the
    #  file it belongs to and nothing collides.
    limit = name_limit(target)
    by_parent: dict[str, list[str]] = {}
    for _path, relative, _is_dir in entries:
        parent, _, name = relative.rpartition("/")
        by_parent.setdefault(parent, []).append(name)
    plans = {parent: plan_names(names, limit)
             for parent, names in by_parent.items()}

    for index, (path, relative, is_dir) in enumerate(entries, start=1):
        progress.check_cancelled()
        parent_path, _, raw_name = relative.rpartition("/")
        parent = dir_blocks.get(parent_path)
        if parent is None:
            continue                      # parent was skipped; skip its contents
        name = plans[parent_path][raw_name]
        if name != raw_name:
            renamed += 1
            progress.log(f"  {relative} -> {name}")
        try:
            if is_dir:
                #  Nothing can already exist on a freshly formatted volume, and
                #  checking would mean walking the directory once per entry.
                dir_blocks[relative] = target.mkdir(parent, name,
                                                    check_existing=False)
            else:
                data = _read_source(path, relative, compat, progress)
                if compat is not None:
                    data = compat.offer(relative, data)
                    if compat.skip(relative):
                        continue
                target.write_file(parent, name, data, check_existing=False)
                copied += 1
        except amigafs.AmigaFsError as error:
            progress.log(f"  skipped {relative}: {error}")
        if index % 200 == 0 or index == len(entries):
            progress.fraction(index / len(entries))
    if renamed:
        progress.log(f"WARNING: {renamed} name(s) had to be shortened to fit "
                     f"{limit} characters. Software that refers to a file by "
                     f"name - a WHDLoad slave, an icon's tool types - may no "
                     f"longer find it. A PFS3 partition avoids this.")
    if compat is not None:
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
            close: bool = True) -> VolumeWriter:
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
            copied, skipped = _copy_volume(source, target,
                                           match.role.destination, progress)
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
