"""Checking whether a newer release of this tool has been published.

Asked for rather than done on every start: a tool that prepares a card should
not be reaching out to the internet unless someone has asked it a question.
The About dialog's Check for Application Updates button is the only thing that
asks, and the menu item of the same name opens the dialog and presses it.

The check reads the one release GitHub marks as the latest, which is never a
draft or a prerelease, and compares its tag, ``vX.Y.Z``, with ``__version__``.
A check that cannot be answered raises UpdateError with the reason: no
network, a reply that cannot be read, a repository with no releases yet. None
of those mean anything is wrong with the copy in front of the user, and none
of them is ever reported as it being up to date.

A release publishes a Debian package for each system it is built for, and a
SHA256SUMS file. A copy installed from one of those packages updates itself:
the package for its system is downloaded, checked against SHA256SUMS, and
installed by ``install-update`` under pkexec, which checks it again as root
before apt is given it (see packaging/install-update). The package records
which system it was built for, in ``package-target`` beside the application.

A git checkout, or a copy pipx installed from a tag, cannot be replaced by the
running program, so a newer release is answered there with the release page
and the command that updates that copy. The tool never runs git or pip itself.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import APPLICATION_NAME, __version__

REPO = "peteclarke-del/pistorm-linux"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases"
LATEST_URL = f"{RELEASES_API}/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
#  What pipx installs from, as the README's install command writes it.
INSTALL_SOURCE = f"git+https://github.com/{REPO}"
USER_AGENT = "pistorm-imager"
GITHUB_JSON = "application/vnd.github+json"
#  The folder the package runs from: a checkout's pistorm_imager, or the copy
#  in an environment's site-packages.
PACKAGE_DIR = Path(__file__).resolve().parents[1]
#  The file pipx writes into every environment it makes.
PIPX_METADATA = "pipx_metadata.json"
#  What a release package installs beside the application: the record of the
#  system it was built for, and the installer pkexec runs as root.
PACKAGE_TARGET = PACKAGE_DIR.parent / "package-target"
INSTALL_HELPER = PACKAGE_DIR.parent / "bin" / "install-update"
CHECKSUMS = "SHA256SUMS"
#  pkexec's exit statuses when the password prompt is dismissed or refused.
PKEXEC_DISMISSED = 126
PKEXEC_REFUSED = 127
CHUNK = 64 * 1024
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class UpdateError(RuntimeError):
    """The check could not be made; the message says why, for the user."""


class UpdateCancelled(UpdateError):
    """The user stopped it: a cancelled download, a dismissed password."""


@dataclasses.dataclass(frozen=True)
class PackageTarget:
    """The system a release package was built for, as it recorded itself."""

    distro: str        # "ubuntu24.04"
    arch: str          # "all"
    package: str       # "PiStorm-Imager_{version}_ubuntu24.04_all.deb"

    def package_name(self, version: str) -> str:
        return self.package.replace("{version}", version)

    @property
    def system(self) -> str:
        """For people: "Ubuntu 24.04"."""
        name = re.match(r"([a-z]+)([\d.]*)", self.distro)
        if name is None:
            return self.distro
        return f"{name.group(1).capitalize()} {name.group(2)}".strip()


@dataclasses.dataclass(frozen=True)
class VerifiedPackage:
    """A downloaded package, and the SHA-256 it was checked against."""

    path: Path
    sha256: str


@dataclasses.dataclass(frozen=True)
class Release:
    """A published release newer than the running version."""

    version: str       # "0.9.0"
    tag: str           # "v0.9.0"
    url: str           # its page on GitHub
    #  This system's package, where the release has one, and the checksums.
    package_name: str = ""
    package_url: str = ""
    package_size: int = 0
    sums_url: str = ""
    notes: str = ""

    @property
    def installable(self) -> bool:
        return bool(self.package_url and self.sums_url)

    @property
    def name(self) -> str:
        #  Release titles here are free text, from "0.7.0" to a sentence
        #  after the tag, so the messages name the release themselves.
        return f"{APPLICATION_NAME} {self.version}"


@dataclasses.dataclass(frozen=True)
class Installation:
    """How the running copy got here, which decides how it is updated.

    ``kind`` is "package" for a copy a release package installed (``target``
    says which), "checkout" for a git working copy (``folder`` is its top),
    "pipx" for a copy pipx installed, and "other" for anything else, such as
    a source tree without git or a plain pip install.
    """

    kind: str
    folder: Path | None = None
    target: PackageTarget | None = None


def parse_version(text: str) -> tuple[int, int, int] | None:
    """(0, 8, 1) for "v0.8.1" or "0.8.1"; None for anything else."""
    text = text or ""
    match = _TAG.match(text if text.startswith("v") else f"v{text}")
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_newer(tag: str, current: str = __version__) -> bool:
    """Whether ``tag`` is a later version than ``current``.

    A tag that is not ``vX.Y.Z`` is never newer: better to say nothing than
    to announce an update that is not one.
    """
    theirs, ours = parse_version(tag), parse_version(current)
    return theirs is not None and (ours is None or theirs > ours)


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url


def fetch_latest(url: str = LATEST_URL, timeout: float = 15) -> dict[str, Any] | None:
    """The release GitHub marks as the latest, or None when none is published.

    Raises UpdateError, with the reason as a sentence, for anything else.
    """
    host = _host(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": GITHUB_JSON})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        message = ""
        try:
            message = str(json.loads(error.read().decode("utf-8")).get("message", ""))
        except (OSError, ValueError, AttributeError):
            pass
        detail = f": {message}" if message else ""
        raise UpdateError(
            f"{host} sent an unexpected reply (HTTP {error.code}{detail}).") from error
    except urllib.error.URLError as error:
        raise UpdateError(f"{host} could not be reached: {error.reason}.") from error
    except OSError as error:            # a timeout or a reset while reading
        reason = str(error) or "timed out"
        raise UpdateError(f"{host} could not be reached: {reason}.") from error
    try:
        release = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise UpdateError(f"The reply from {host} could not be read.") from error
    if not isinstance(release, dict) or "tag_name" not in release:
        raise UpdateError(f"{host} did not send a release.")
    return release


#  How much of a release's notes the update shows.
NOTES_LIMIT = 2000


def release_from(release: dict[str, Any], current: str = __version__,
                 target: PackageTarget | None = None) -> Release | None:
    """The release when it is newer than ``current``, else None.

    With ``target``, the package the release publishes for that system, and
    its checksums, are found among its assets.

    Raises UpdateError when the latest release is not tagged ``vX.Y.Z``,
    which would be a mistake in publishing it.
    """
    tag = str(release.get("tag_name") or "")
    version = parse_version(tag)
    if version is None:
        raise UpdateError(f"The latest release on GitHub, {tag or 'without a tag'}, "
                          "is not tagged with a version number.")
    if not is_newer(tag, current):
        return None
    number = ".".join(str(part) for part in version)
    assets = {str(asset.get("name")): asset
              for asset in release.get("assets") or []
              if isinstance(asset, dict)}
    wanted = target.package_name(number) if target is not None else ""
    package = assets.get(wanted) if wanted else None
    sums = assets.get(CHECKSUMS)
    notes = str(release.get("body") or "").strip()
    if len(notes) > NOTES_LIMIT:
        notes = notes[:NOTES_LIMIT].rstrip() + "..."
    return Release(
        version=number, tag=tag,
        url=str(release.get("html_url") or RELEASES_PAGE),
        package_name=wanted,
        package_url=str(package.get("browser_download_url") or "") if package else "",
        package_size=int(package.get("size") or 0) if package else 0,
        sums_url=str(sums.get("browser_download_url") or "") if sums else "",
        notes=notes)


def check(current: str = __version__,
          fetch: Callable[[str], dict[str, Any] | None] = fetch_latest,
          target: PackageTarget | None = None) -> Release | None:
    """A newer release than ``current``, or None when this is the newest.

    ``target`` is the system this copy's package was built for, if it came
    from one; the release's package for it is then looked for.

    Raises UpdateError, with the reason, when the check cannot be made.
    """
    release = fetch(LATEST_URL)
    if release is None:
        raise UpdateError("No release has been published on GitHub yet.")
    return release_from(release, current, target)


def installed_target(path: Path = PACKAGE_TARGET) -> PackageTarget | None:
    """The system the installed package was built for, or None.

    None for a copy no release package installed - a checkout, a pipx or pip
    install - and for a record that cannot be read.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    fields = dict(line.split("=", 1) for line in lines if "=" in line)
    distro, arch, package = (fields.get(key, "").strip()
                             for key in ("distro", "arch", "package"))
    if not (distro and arch and package and "{version}" in package
            and package.endswith(".deb") and "/" not in package):
        return None
    return PackageTarget(distro, arch, package)


def installation(package_dir: Path = PACKAGE_DIR,
                 prefix: str = sys.prefix) -> Installation:
    """How the copy in ``package_dir`` was put there."""
    top = package_dir.parent
    target = installed_target(top / PACKAGE_TARGET.name)
    if target is not None:
        return Installation("package", target=target)
    #  A file rather than a folder in a linked worktree.
    if (top / ".git").exists():
        return Installation("checkout", top)
    if (Path(prefix) / PIPX_METADATA).is_file():
        return Installation("pipx")
    return Installation("other")


def update_command(release: Release, where: Installation) -> str:
    """The command that updates this copy to ``release``, or "" when there is none."""
    if where.kind == "checkout" and where.folder is not None:
        return shlex.join(["git", "-C", str(where.folder), "pull"])
    if where.kind == "pipx":
        return shlex.join(["pipx", "install", "--force", "--system-site-packages",
                           f"{INSTALL_SOURCE}@{release.tag}"])
    return ""


def how_to_update(release: Release, where: Installation) -> str:
    """A sentence on how this copy is updated, for the About dialog."""
    if where.kind == "package" and where.target is not None:
        if release.installable:
            return "It can be downloaded and installed from here."
        return (f"The release has no package for {where.target.system}, so this "
                f"copy cannot update itself; the release page has what there is.")
    command = update_command(release, where)
    if where.kind == "checkout":
        return ("This copy runs from a git checkout, so it cannot update itself. "
                f"Update it in a terminal with: {command}")
    if where.kind == "pipx":
        return ("This copy was installed with pipx, so it cannot update itself. "
                f"Update it in a terminal with: {command}")
    return ("This copy cannot update itself. Install the new release the way this "
            "copy was installed; the release page has its source.")


def download_folder() -> Path:
    """Where updates are downloaded: this user's cache, not root's."""
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "pistorm-imager" / "updates"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def published_sum(sums: str, name: str) -> str:
    """The SHA-256 SHA256SUMS gives for ``name``, as sha256sum writes it."""
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name \
                and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    return ""


def _open(url: str, timeout: float = 30):
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise UpdateError(f"Not a web address: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise UpdateError(f"{_host(url)} sent an unexpected reply "
                          f"(HTTP {error.code}).") from error
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", None) or str(error) or "timed out"
        raise UpdateError(f"{_host(url)} could not be reached: {reason}.") from error


def download(release: Release,
             progress: Callable[[int, int | None], None] | None = None,
             cancel: threading.Event | None = None,
             folder: Path | None = None) -> VerifiedPackage:
    """Download this system's package for ``release``, checked against its sum.

    A copy already there that matches is used again, so a second press after
    a dismissed password prompt does not fetch it twice. Nothing that fails
    the check, or is cut short, is left behind.
    """
    if not release.installable:
        raise UpdateError(f"{release.name} has no package for this system.")
    folder = folder or download_folder()
    folder.mkdir(parents=True, exist_ok=True)
    with _open(release.sums_url) as response:
        sums = response.read(64 * 1024).decode("utf-8", "replace")
    expected = published_sum(sums, release.package_name)
    if not expected:
        raise UpdateError(f"{CHECKSUMS} does not list {release.package_name}, "
                          f"so it was not downloaded.")
    target = folder / release.package_name
    if target.is_file() and _sha256(target) == expected:
        return VerifiedPackage(target, expected)
    part = target.with_name(target.name + ".part")
    try:
        with _open(release.package_url) as response, open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0) or None
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateCancelled("The download was cancelled.")
                block = response.read(CHUNK)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if progress is not None:
                    progress(done, total)
        if total is not None and done != total:
            raise UpdateError("The download was cut short.")
        if _sha256(part) != expected:
            raise UpdateError(f"{release.package_name} does not match its "
                              f"published checksum, so it was not kept.")
        part.replace(target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    for other in folder.glob("*.deb"):
        if other != target:
            other.unlink(missing_ok=True)
    return VerifiedPackage(target, expected)


def by_hand(package: Path) -> str:
    return f"Install it in a terminal with: sudo apt install {shlex.quote(str(package))}"


def install_command(package: VerifiedPackage,
                    helper: Path = INSTALL_HELPER) -> list[str] | None:
    """pkexec running the installer on ``package``, or None without either."""
    pkexec = shutil.which("pkexec")
    if not (pkexec and helper.is_file()):
        return None
    return [pkexec, str(helper), str(package.path), package.sha256]


def install(package: VerifiedPackage, run=subprocess.run,
            helper: Path = INSTALL_HELPER) -> None:
    """Install ``package`` as root, after the root side has checked it again.

    No time limit: pkexec waits as long as its password prompt is open, and
    once answered apt runs as root, where this cannot stop it - giving up
    would say it failed while apt went on to finish.
    """
    command = install_command(package, helper)
    if command is None:
        raise UpdateError("pkexec or the package's installer is missing, so the "
                          "update cannot be installed from here. "
                          + by_hand(package.path))
    result = run(command, capture_output=True, text=True, check=False)
    if result.returncode == PKEXEC_DISMISSED:
        raise UpdateCancelled("The password prompt was dismissed, so nothing "
                              "was installed.")
    if result.returncode == PKEXEC_REFUSED:
        raise UpdateError("The system did not allow the installation. "
                          + by_hand(package.path))
    if result.returncode:
        said = [line for line in (result.stderr or "").splitlines() if line.strip()]
        reason = said[-1] if said else f"the installer exited with {result.returncode}"
        raise UpdateError(f"The update was not installed: {reason}. "
                          + by_hand(package.path))
    package.path.unlink(missing_ok=True)
