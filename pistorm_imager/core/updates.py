"""Checking whether a newer release of this tool has been published.

Asked for rather than done on every start: a tool that prepares a card should
not be reaching out to the internet unless someone has asked it a question.

The check is deliberately forgiving. A network that is not there, an API that
has changed, a repository with no releases yet - none of those are worth an
error, because none of them mean anything is wrong with the copy in front of
the user. They mean the question could not be answered.
"""
from __future__ import annotations

import dataclasses
import json
import re
import urllib.error
import urllib.request

from .. import __version__

REPO = "peteclarke-del/pistorm-linux"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
USER_AGENT = "pistorm-imager"


@dataclasses.dataclass(frozen=True)
class Release:
    tag: str
    name: str
    notes: str
    url: str

    @property
    def version(self) -> tuple[int, ...]:
        return parse_version(self.tag)


def parse_version(text: str) -> tuple[int, ...]:
    """The numbers in a version string, for comparing one against another.

    Tags in the wild carry a "v", a suffix, or both; only the numbers decide
    the order, and a tag with none of them sorts as nothing at all.
    """
    numbers = re.findall(r"\d+", text or "")
    return tuple(int(number) for number in numbers[:4])


def is_newer(candidate: str, current: str = __version__) -> bool:
    """Whether ``candidate`` is a later version than ``current``."""
    theirs, ours = parse_version(candidate), parse_version(current)
    if not theirs:
        return False
    #  Pad so 0.3 and 0.3.0 compare equal rather than by length.
    width = max(len(theirs), len(ours))
    theirs += (0,) * (width - len(theirs))
    ours += (0,) * (width - len(ours))
    return theirs > ours


def latest(timeout: int = 15) -> Release | None:
    """The newest published release, or None if that cannot be established."""
    request = urllib.request.Request(RELEASES_API,
                                     headers={"User-Agent": USER_AGENT,
                                              "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if not isinstance(payload, list):
        return None
    published = [entry for entry in payload
                 if isinstance(entry, dict) and not entry.get("draft")
                 and not entry.get("prerelease")]
    if not published:
        return None
    newest = max(published,
                 key=lambda entry: parse_version(entry.get("tag_name", "")))
    tag = newest.get("tag_name", "")
    return Release(tag=tag,
                   name=newest.get("name") or tag,
                   notes=(newest.get("body") or "").strip(),
                   url=newest.get("html_url") or RELEASES_PAGE)
