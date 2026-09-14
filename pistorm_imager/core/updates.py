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

A release publishes no package, only its tag. The tool runs from a git
checkout, or is installed from a tag with pipx, and neither can be replaced by
the running program, so a newer release is answered with the release page and
the command that updates this particular copy. The tool never runs git or pip
itself.
"""
from __future__ import annotations

import dataclasses
import json
import re
import shlex
import sys
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
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class UpdateError(RuntimeError):
    """The check could not be made; the message says why, for the user."""


@dataclasses.dataclass(frozen=True)
class Release:
    """A published release newer than the running version."""

    version: str       # "0.9.0"
    tag: str           # "v0.9.0"
    url: str           # its page on GitHub

    @property
    def name(self) -> str:
        #  Release titles here are free text, from "0.7.0" to a sentence
        #  after the tag, so the messages name the release themselves.
        return f"{APPLICATION_NAME} {self.version}"


@dataclasses.dataclass(frozen=True)
class Installation:
    """How the running copy got here, which decides how it is updated.

    ``kind`` is "checkout" for a git working copy (``folder`` is its top),
    "pipx" for a copy pipx installed, and "other" for anything else, such as
    a source tree without git or a plain pip install.
    """

    kind: str
    folder: Path | None = None


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


def release_from(release: dict[str, Any], current: str = __version__) -> Release | None:
    """The release when it is newer than ``current``, else None.

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
    return Release(version=".".join(str(part) for part in version), tag=tag,
                   url=str(release.get("html_url") or RELEASES_PAGE))


def check(current: str = __version__,
          fetch: Callable[[str], dict[str, Any] | None] = fetch_latest) -> Release | None:
    """A newer release than ``current``, or None when this is the newest.

    Raises UpdateError, with the reason, when the check cannot be made.
    """
    release = fetch(LATEST_URL)
    if release is None:
        raise UpdateError("No release has been published on GitHub yet.")
    return release_from(release, current)


def installation(package_dir: Path = PACKAGE_DIR,
                 prefix: str = sys.prefix) -> Installation:
    """How the copy in ``package_dir`` was put there."""
    top = package_dir.parent
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
    command = update_command(release, where)
    if where.kind == "checkout":
        return ("This copy runs from a git checkout, so it cannot update itself. "
                f"Update it in a terminal with: {command}")
    if where.kind == "pipx":
        return ("This copy was installed with pipx, so it cannot update itself. "
                f"Update it in a terminal with: {command}")
    return ("This copy cannot update itself. Install the new release the way this "
            "copy was installed; the release page has its source.")
