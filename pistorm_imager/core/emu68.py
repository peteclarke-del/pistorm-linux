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
import json
import os
import re
import shutil
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


def cache_dir() -> Path:
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
