"""Fetching and unpacking Emu68 releases (and the Raspberry Pi boot firmware).

Release assets have been renamed over time, which matters because picking the
wrong one produces a card that simply will not boot:

* up to and including 1.0.7  ``Emu68-pistorm.zip`` was the *classic* PiStorm and
  ``Emu68-pistorm32lite.zip`` the PiStorm32-lite build;
* from 1.1 onwards ``Emu68-pistorm.zip`` covers the FPGA boards (PiStorm32-lite
  and PiStorm16) while the CPLD board moved to ``Emu68-pistorm-classic.zip``.

:func:`asset_for` encodes that, preferring an exact match and falling back to a
pattern search so that future renames degrade into a warning rather than a crash.
"""
from __future__ import annotations

import dataclasses
import gzip
import json
import os
import re
import shutil
import struct
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .util import Progress, human_size

GITHUB_API = "https://api.github.com/repos/michalsc/Emu68/releases"
FIRMWARE_BASE = "https://raw.githubusercontent.com/raspberrypi/firmware/stable/boot/"
USER_AGENT = "pistorm-imager/1.0 (+https://github.com/)"

#  Files the Raspberry Pi needs to start at all.  Emu68 releases from 1.1 no
#  longer bundle them, so we fetch them from the official firmware repository.
FIRMWARE_FILES = [
    "bootcode.bin", "fixup.dat", "fixup4.dat", "start.elf", "start4.elf",
    "bcm2710-rpi-3-b.dtb", "bcm2710-rpi-3-b-plus.dtb", "bcm2710-rpi-cm3.dtb",
    "bcm2710-rpi-zero-2-w.dtb", "bcm2711-rpi-4-b.dtb", "bcm2711-rpi-400.dtb",
    "bcm2711-rpi-cm4.dtb", "LICENCE.broadcom",
]


@dataclasses.dataclass(frozen=True)
class Variant:
    key: str
    label: str
    description: str
    #  Asset name per release-name era; the first match wins.
    modern_asset: str
    legacy_asset: str

    def asset_names(self) -> list[str]:
        return [self.modern_asset, self.legacy_asset]


VARIANTS = [
    Variant(
        key="pistorm32lite",
        label="PiStorm32-lite / PiStorm16",
        description="FPGA boards: PiStorm32-lite (A1200) and PiStorm16 (A600)",
        modern_asset="Emu68-pistorm.zip",
        legacy_asset="Emu68-pistorm32lite.zip",
    ),
    Variant(
        key="pistorm",
        label="PiStorm (classic)",
        description="Original CPLD PiStorm for A500/A500+/A1000/A2000",
        modern_asset="Emu68-pistorm-classic.zip",
        legacy_asset="Emu68-pistorm.zip",
    ),
    Variant(
        key="raspi",
        label="Bare Raspberry Pi",
        description="Emu68 without PiStorm hardware (no Amiga chipset)",
        modern_asset="Emu68-raspi.zip",
        legacy_asset="Emu68-raspi.zip",
    ),
]

VARIANTS_BY_KEY = {v.key: v for v in VARIANTS}


@dataclasses.dataclass
class Release:
    tag: str
    name: str
    prerelease: bool
    published: str
    assets: dict[str, tuple[str, int]]  # name -> (url, size)

    @property
    def is_modern(self) -> bool:
        """True for 1.1 and later, where the asset naming changed."""
        return "Emu68-pistorm-classic.zip" in self.assets

    def display(self) -> str:
        suffix = "  (pre-release)" if self.prerelease else ""
        return f"{self.name or self.tag}{suffix}"


#  A release tag: "v1.1.0-beta.1", "v1.0.7", "v1.0-rc.3".  Only the numbers
#  before the pre-release suffix are compared, because a requirement written
#  as "1.1 alpha.1 or later" is satisfied by every 1.1 build there is - the
#  alphas and betas included, which is all that exists of 1.1 so far.
_VERSION = re.compile(r"v?(\d+(?:\.\d+)*)")


def version_of(tag: str) -> tuple[int, ...]:
    """The numeric version in a release tag, or ``()`` when there is none.

    An empty tuple means *unknown*, which is a real answer here: a build can
    be made from a zip on disk or from an already-unpacked folder, and neither
    carries a tag for anything to read.
    """
    found = _VERSION.match(tag.strip())
    if not found:
        return ()
    return tuple(int(part) for part in found.group(1).split("."))


def at_least(tag: str, minimum: tuple[int, ...]) -> bool:
    """Whether ``tag`` names a release at or after ``minimum``.

    An unreadable or absent tag answers ``True``.  The alternative - refusing
    what cannot be checked - would hide software from anybody building against
    a local zip, and the build says plainly what it could not verify instead.
    """
    version = version_of(tag)
    if not version:
        return True
    padded = version + (0,) * (len(minimum) - len(version))
    return padded >= minimum


def _urlopen(url: str, timeout: int = 30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_releases(limit: int = 12, include_prereleases: bool = True) -> list[Release]:
    """Query the GitHub API for available Emu68 releases."""
    with _urlopen(f"{GITHUB_API}?per_page={limit}") as response:
        payload = json.loads(response.read().decode("utf-8"))
    releases = []
    for item in payload:
        if item.get("draft"):
            continue
        if item.get("prerelease") and not include_prereleases:
            continue
        assets = {a["name"]: (a["browser_download_url"], a["size"])
                  for a in item.get("assets", [])}
        releases.append(Release(
            tag=item["tag_name"],
            name=item.get("name") or item["tag_name"],
            prerelease=bool(item.get("prerelease")),
            published=(item.get("published_at") or "")[:10],
            assets=assets,
        ))
    return releases


def asset_for(release: Release, variant_key: str) -> str:
    """Choose the download asset for a board variant within a release."""
    variant = VARIANTS_BY_KEY[variant_key]
    if release.is_modern:
        candidates = [variant.modern_asset]
    else:
        candidates = [variant.legacy_asset]
    #  Accept the other spelling too, in case a release straddles the rename.
    candidates += [n for n in variant.asset_names() if n not in candidates]
    for name in candidates:
        if name in release.assets:
            return name
    #  Last resort: anything that mentions the board.
    pattern = re.compile(re.escape(variant.key), re.IGNORECASE)
    for name in release.assets:
        if pattern.search(name.replace("-", "")):
            return name
    raise LookupError(
        f"release {release.tag} has no asset for {variant.label} "
        f"(available: {', '.join(sorted(release.assets)) or 'none'})"
    )


#  Set when a build has been told which cache to use, because it is running
#  as somebody else. See ``use_cache``.
_CACHE: Path | None = None


def use_cache(path: str | Path) -> None:
    """Use this cache rather than the one belonging to whoever is running.

    Writing to a card runs the build under ``pkexec``, so it runs as root and
    ``Path.home()`` becomes ``/root``. The user's archives were then invisible:
    every package was downloaded again into root's cache, and **Roadshow was
    left off the card entirely**, because its publisher serves it only to a
    browser and the copy that would have satisfied it was in the user's cache
    where the privileged build could not see it. So writing to a file and
    writing to a card produced different cards from the same choices.

    ``pkexec`` sanitises the environment, so this cannot travel as a variable;
    it goes in the job file with the rest of the build, and is applied here.
    A path that is not there is ignored rather than obeyed, so a saved setup
    carried to another machine falls back to that machine's own cache.
    """
    global _CACHE                                            # noqa: PLW0603
    wanted = Path(path)
    _CACHE = wanted if wanted.is_dir() else None


#  The release this build is installing, when it is installing one.  Emu68 1.1
#  began publishing ``VideoCore.card`` as a release asset of its own, and that
#  copy is newer than the one inside Emu68-tools - 1.5 against 1.3 at the time
#  of writing - so the RTG driver that goes onto a card should come from the
#  release the card boots rather than from a tools archive with its own
#  release schedule.
#
#  Kept here beside the cache for the same reason that is: the code that needs
#  it is several layers away from the code that resolves it, and threading a
#  release object through every one of them to reach the compatibility pass
#  would put the fact in a dozen signatures that have no other use for it.
_RELEASE: "Release | None" = None


def use_release(release: "Release | None") -> None:
    """Remember the Emu68 release this build installs, for what rides with it."""
    global _RELEASE                                          # noqa: PLW0603
    _RELEASE = release


def release_in_use() -> "Release | None":
    """The release being installed, or ``None`` when Emu68 is not being installed.

    ``None`` is a real answer: a card can be updated without reinstalling
    Emu68, and a build can be made from a local zip, and in neither case is
    there a release for anything to take a file out of.
    """
    return _RELEASE


def cache_dir() -> Path:
    if _CACHE is not None:
        return _CACHE
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    path = base / "pistorm-imager"
    path.mkdir(parents=True, exist_ok=True)
    return path


def download(url: str, destination: Path, expected_size: int | None,
             progress: Progress) -> Path:
    """Download to ``destination``, reusing a complete cached copy if present."""
    if destination.exists() and expected_size and destination.stat().st_size == expected_size:
        progress.log(f"Using cached {destination.name} ({human_size(expected_size)})")
        return destination
    progress.log(f"Downloading {url}")
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with _urlopen(url, timeout=60) as response, open(temporary, "wb") as out:
            total = expected_size or int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                progress.check_cancelled()
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    progress.fraction(done / total)
    except urllib.error.URLError as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"download failed: {error}") from error
    temporary.replace(destination)
    progress.log(f"Downloaded {destination.name} ({human_size(destination.stat().st_size)})")
    return destination


def get_release_archive(release: Release, variant_key: str, progress: Progress) -> Path:
    name = asset_for(release, variant_key)
    url, size = release.assets[name]
    target = cache_dir() / f"{release.tag}-{name}"
    return download(url, target, size, progress)


def extract(archive: Path, destination: Path, progress: Progress) -> list[Path]:
    """Unpack a release zip, flattening nothing and skipping directory entries."""
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        for index, member in enumerate(members, start=1):
            progress.check_cancelled()
            #  Refuse absolute or traversing paths from an untrusted archive.
            relative = Path(member.filename.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe path in archive: {member.filename}")
            out = destination / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(out)
            progress.fraction(index / len(members))
    return written


def needs_firmware(files: list[Path]) -> bool:
    """True when a release does not bundle the Raspberry Pi boot firmware."""
    names = {p.name for p in files}
    return "start4.elf" not in names and "start.elf" not in names


def fetch_firmware(destination: Path, progress: Progress) -> list[Path]:
    """Download the Raspberry Pi boot files Emu68 1.1+ no longer bundles."""
    destination.mkdir(parents=True, exist_ok=True)
    cache = cache_dir() / "rpi-firmware"
    cache.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for index, name in enumerate(FIRMWARE_FILES, start=1):
        progress.check_cancelled()
        cached = cache / name
        #  Kept only if it came from where this build is asking, and only if
        #  all of it arrived: a download that stopped early is still a file,
        #  and cached by existence alone it would go on every card built
        #  afterwards.
        note = cached.with_name(cached.name + ".source")
        source = FIRMWARE_BASE + name
        good = (cached.exists() and cached.stat().st_size > 0
                and note.exists() and note.read_text().strip() == source)
        if not good:
            try:
                with _urlopen(source, timeout=60) as response:
                    declared = int(response.headers.get("Content-Length") or 0)
                    data = response.read()
                if declared and len(data) != declared:
                    raise RuntimeError(
                        f"{name} arrived {len(data)} bytes long, not the "
                        f"{declared} the server declared")
                cached.write_bytes(data)
                note.write_text(source + "\n")
            except (urllib.error.URLError, RuntimeError) as error:
                raise RuntimeError(
                    f"could not download Raspberry Pi firmware file {name}: {error}"
                ) from error
        target = destination / name
        shutil.copyfile(cached, target)
        out.append(target)
        progress.fraction(index / len(FIRMWARE_FILES))
        progress.log(f"Raspberry Pi firmware: {name}")
    return out


def kernel_name(files: list[Path]) -> str | None:
    """Return the Emu68 kernel file name found in an unpacked release."""
    for path in files:
        if path.name.startswith("Emu68-") and path.suffix in ("", ".gz", ".img"):
            return path.name
    return None


def has_variant(release: Release, variant_key: str) -> bool:
    """Whether ``release`` ships a build for this board (some early ones do not)."""
    try:
        asset_for(release, variant_key)
        return True
    except LookupError:
        return False


#  ------------------------------------------------------------------ overlays
#
#  Emu68 1.1 moved its settings out of cmdline.txt and into device tree
#  overlays, and says so in its own ``overlays/overlays.md``: "Starting with
#  Emu68 1.1 the use of cmdline.txt for adjusting Emu68 parameters is
#  obsolete."  Several switches went further than obsolete - ``unicam.boot``,
#  ``unicam.smooth``, ``vbr_move``, ``z2_ram_size`` and ``sd.unit0`` are not in
#  the 1.1 kernel at all, so a card written with them says nothing and does
#  nothing.  Which form to write is therefore not a matter of taste.
#
#  The release is asked what it carries rather than what it is called.  A
#  build can be made from a zip on disk or an already-unpacked folder, neither
#  of which has a tag for anything to read, and a tag is the wrong question
#  anyway: what decides the answer is whether the overlay files are there to
#  be loaded.

OVERLAY_SUFFIX = ".dtbo"


def overlays_in(files) -> frozenset[str]:
    """The device tree overlays an unpacked release ships, without extensions.

    ``files`` is anything iterable of paths - the list :func:`extract` returns,
    or a directory listing read back off a card.
    """
    names = set()
    for item in files:
        name = getattr(item, "name", str(item))
        if name.lower().endswith(OVERLAY_SUFFIX):
            names.add(name[:-len(OVERLAY_SUFFIX)])
    return frozenset(names)


def overlay_path(files, name: str) -> Path | None:
    """The file behind an overlay name, when the release's files are to hand."""
    wanted = (name + OVERLAY_SUFFIX).lower()
    for item in files:
        path = Path(item)
        if path.name.lower() == wanted:
            return path
    return None


#  Flattened device tree constants.  Only enough of the format to read the
#  parameter names out of a compiled overlay is implemented here.
_FDT_MAGIC = 0xD00DFEED
_FDT_BEGIN_NODE, _FDT_END_NODE, _FDT_PROP, _FDT_NOP, _FDT_END = 1, 2, 3, 4, 9


def overlay_parameters(path: Path) -> frozenset[str]:
    """The parameter names a compiled overlay accepts, read out of the file.

    A parameter spelled wrongly does nothing at all and says nothing about it -
    which is exactly how ``enable_c0_slow`` went missing from every card this
    tool wrote.  The overlay itself lists the names it answers to, in its
    ``__overrides__`` node, so they can be checked rather than remembered.

    An unreadable or unexpected file returns no names, which the caller reads
    as "cannot check" rather than as "wrong".
    """
    try:
        data = path.read_bytes()
    except OSError:
        return frozenset()
    if len(data) < 40 or struct.unpack_from(">I", data, 0)[0] != _FDT_MAGIC:
        return frozenset()
    struct_off, strings_off = struct.unpack_from(">II", data, 8)
    names: set[str] = set()
    depth, in_overrides = 0, 0
    offset = struct_off
    while offset + 4 <= len(data):
        token = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if token == _FDT_BEGIN_NODE:
            end = data.index(b"\0", offset)
            node = data[offset:end].decode("utf-8", errors="replace")
            offset = (end + 4) & ~3
            depth += 1
            if node == "__overrides__":
                in_overrides = depth
        elif token == _FDT_END_NODE:
            if in_overrides == depth:
                in_overrides = 0
            depth -= 1
        elif token == _FDT_PROP:
            length, name_off = struct.unpack_from(">II", data, offset)
            offset += 8
            if in_overrides:
                start = strings_off + name_off
                end = data.index(b"\0", start)
                names.add(data[start:end].decode("utf-8", errors="replace"))
            offset = (offset + length + 3) & ~3
        elif token == _FDT_NOP:
            continue
        else:                       # FDT_END, or something unrecognised
            break
    return frozenset(names)


#  ------------------------------------------------------- kernels from a fork
#
#  Emu68's JIT has an unmerged change - "dcache range extensions" - that makes
#  the cache housekeeping around every hardware transfer cheap. The driver
#  stack's own measurements put gigabit Ethernet at 104 Mbit/s in on an
#  official Emu68 and 698 with the extensions, so for anybody using the Pi's
#  network socket it is the difference between a network card and a fast one.
#
#  It is published as a kernel and nothing else: no firmware, no config.txt, no
#  overlays. So it is not another release to choose instead - it is a kernel
#  laid over an official release, which still supplies everything else on the
#  boot partition.


@dataclasses.dataclass(frozen=True)
class Kernel:
    """An Emu68 kernel published somewhere other than the official release."""

    key: str
    label: str
    description: str
    repo: str
    tag: str
    #  Board variant -> the asset carrying that board's kernel.  A board with
    #  no entry cannot have this kernel at all.
    assets: dict
    #  The oldest official release it can be laid over.
    min_release: tuple
    #  Which build of the Emu68 driver stack belongs with it, for drivers
    #  compiled against these extensions.
    driver_flavour: str = ""
    notes: tuple = ()


KERNELS = [
    Kernel(
        key="rangeops",
        label="Experimental kernel with the dcache extensions",
        description=(
            "An unofficial Emu68 build carrying a JIT change that has not "
            "been merged upstream. It makes the cache housekeeping around "
            "every hardware transfer cheap, which is what the Raspberry Pi's "
            "drivers spend their time on: its authors measure the Pi's "
            "Ethernet socket at 104 Mbit/s on an official Emu68 and 698 with "
            "this."),
        repo="rondoval/Emu68",
        tag="v1.1-alpha-with-rangeops",
        assets={"pistorm32lite": "Emu68-pistorm.gz",
                "pistorm": "Emu68-pistorm-classic.gz"},
        min_release=(1, 1),
        driver_flavour="rangeops",
        notes=(
            "Experimental, and unofficial: it is one person's branch of "
            "Emu68, not a release of it.",
            "Only the kernel is replaced. The firmware, the device tree, the "
            "overlays and config.txt all come from the official release "
            "chosen beside it.",
            "There is no bare Raspberry Pi build of it, so it is offered only "
            "for the PiStorm boards.",
        ),
    ),
]

KERNELS_BY_KEY = {k.key: k for k in KERNELS}


def kernel_suits(kernel: Kernel, variant: str, release_tag: str) -> bool:
    """Whether this kernel can be laid over that release, for that board."""
    return variant in kernel.assets and at_least(release_tag or "",
                                                 kernel.min_release)


def kernel_asset(kernel: Kernel, variant: str) -> tuple[str, int]:
    """The download for a board's kernel, asked of the publisher.

    The address is not written down here: the tag is, and the asset is found
    in the release under it, so a renamed asset is a clear failure rather than
    a download of something that is not there.
    """
    wanted = kernel.assets.get(variant)
    if wanted is None:
        raise LookupError(f"{kernel.label} has no build for this board")
    url = f"https://api.github.com/repos/{kernel.repo}/releases"
    with _urlopen(url) as response:
        payload = json.loads(response.read().decode("utf-8"))
    for item in payload:
        if item.get("tag_name") != kernel.tag:
            continue
        for asset in item.get("assets", []):
            if asset["name"] == wanted:
                return asset["browser_download_url"], asset["size"]
    raise LookupError(f"{kernel.tag} has no asset named {wanted}")


def kernel_version(path: Path) -> str:
    """The version string inside a kernel file, read out of the file itself.

    A kernel is chosen by name and a name proves nothing, so the card's log
    says which Emu68 actually went onto it. The file is gzipped, and small
    enough to read whole.
    """
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as handle:
                data = handle.read()
        else:
            data = path.read_bytes()
    except OSError:
        return ""
    found = re.search(rb"\$VER: (Emu68 [^\x00]{0,60})", data)
    return found.group(1).decode("utf-8", errors="replace").strip() if found else ""


def install_kernel(kernel: Kernel, variant: str, files: list[Path],
                   destination: Path, progress: Progress) -> list[Path]:
    """Put a kernel from a fork in place of the one the release shipped.

    The file keeps the release's own name, so ``kernel=`` in config.txt still
    names the file that is there. Both publishers happen to use the same
    names today; taking the release's name rather than the asset's means that
    staying true even if one of them renames.
    """
    url, size = kernel_asset(kernel, variant)
    cached = cache_dir() / f"{kernel.tag}-{Path(url).name}"
    download(url, cached, size, progress)
    official = [p for p in files if kernel_name([p]) == p.name]
    target = destination / (official[0].name if official else Path(url).name)
    shutil.copyfile(cached, target)
    version = kernel_version(target)
    progress.log(f"Kernel replaced with {kernel.label}"
                 + (f": {version}" if version else ""))
    return [p for p in files if p != target] + [target]
