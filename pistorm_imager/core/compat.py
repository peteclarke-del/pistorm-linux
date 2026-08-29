"""Making an emulator's AmigaOS installation work on PiStorm hardware.

A system built for Amiberry or WinUAE carries that emulator's drivers.  They are
ordinary Amiga software, so nothing about AmigaOS 3.9, Scalos or a Kickstart ROM
needs to change - but a driver for a graphics card that does not exist on the
target will simply fail to open, leaving Workbench with nowhere to appear.

These fixes are applied automatically during an install.  Each one is reported,
and none of them touch user data: they add drivers, retarget an icon's tool
types, and comment out commands that can only work under an emulator.
"""
from __future__ import annotations

import dataclasses
import zipfile
from pathlib import Path

from . import amigainfo, emu68
from .util import Progress

#  Picasso96 finds its board through the BOARDTYPE tool type of the icon in
#  DEVS:Monitors, and loads LIBS:Picasso96/<BOARDTYPE>.card to drive it.
EMU68_BOARD = "VideoCore"
EMU68_CARD = "VideoCore.card"
EMU68_TOOLS_URL = ("https://github.com/michalsc/Emu68-tools/releases/download/"
                   "v1.1/Emu68-tools.zip")

#  RTG drivers belonging to emulators.  Their presence is harmless but useless;
#  the monitor icon that selects them is what has to be retargeted.
EMULATOR_CARDS = {"uaegfx.card", "picasso96.card"}
EMULATOR_MONITORS = {"uaegfx"}

#  Commands that only exist inside an emulator.  Left in place they produce a
#  failed command and, with a strict FAILAT, can stop the startup sequence.
EMULATOR_COMMANDS = [
    "uae-configuration", "uaequit", "uaectrl", "uae-control",
    "amiberry_", "uaehf", "uaescsi",
]

STARTUP_FILES = ["S/Startup-Sequence", "S/User-Startup"]

#  A saved screen mode points at a specific display board.  Carried over to a
#  machine with no RTG, it opens Workbench on a screen that does not exist;
#  dropping it makes Workbench fall back to a native mode.
SCREENMODE_PREFS = ["prefs/env-archive/sys/screenmode.prefs",
                    "devs/env-archive/sys/screenmode.prefs"]


@dataclasses.dataclass
class Fix:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def fetch_videocore_card(progress: Progress) -> bytes | None:
    """Get Emu68's RTG driver, from the cache when we already have it."""
    cache = emu68.cache_dir() / EMU68_CARD
    if cache.exists() and cache.stat().st_size > 0:
        return cache.read_bytes()
    archive = emu68.cache_dir() / "Emu68-tools.zip"
    try:
        emu68.download(EMU68_TOOLS_URL, archive, None, progress)
        with zipfile.ZipFile(archive) as zf:
            member = next((n for n in zf.namelist()
                           if Path(n).name.lower() == EMU68_CARD.lower()), None)
            if member is None:
                return None
            data = zf.read(member)
        cache.write_bytes(data)
        return data
    except Exception as error:  # noqa: BLE001 - offline is not fatal
        progress.log(f"Could not obtain {EMU68_CARD}: {error}")
        return None


class Compatibility:
    """Decides what to skip, rewrite and add while filling a volume."""

    def __init__(self, progress: Progress, enabled: bool = True,
                 rtg: bool = True):
        self._pending_data: bytes = b""
        self.progress = progress
        self.enabled = enabled
        #  Whether the target is being watched on an RTG display at all.  With
        #  no RTG there is nothing to substitute the driver *for*, and the
        #  emulator's graphics setup has to come out rather than be replaced.
        self.rtg = rtg
        self.fixes: list[Fix] = []
        #  Known-good copies of files, indexed by lower-case name, used when the
        #  source cannot be read.  A drive image served over a loop mount or a
        #  network share does occasionally throw a transient read error, and
        #  quietly omitting a library is far worse than saying so.
        self.spares: dict[str, Path] = {}
        #  Remembered so the replacement monitor can be built from the original.
        self.monitor_file: bytes | None = None
        self.monitor_icon: bytes | None = None
        self._seen_picasso = False

    def add_spares(self, folder: str | Path) -> int:
        """Index replacement files that can stand in for unreadable sources."""
        folder = Path(folder)
        if not folder.is_dir():
            return 0
        added = 0
        for path in folder.rglob("*"):
            if path.is_file():
                self.spares.setdefault(path.name.lower(), path)
                added += 1
        return added

    def spare_for(self, relative: str) -> Path | None:
        return self.spares.get(Path(relative).name.lower())

    def note(self, kind: str, detail: str) -> None:
        fix = Fix(kind, detail)
        self.fixes.append(fix)
        self.progress.log(f"  compatibility - {fix}")

    # ------------------------------------------------------------ decisions

    def skip(self, relative: str) -> bool:
        """True when a file should not be copied to the target at all."""
        if not self.enabled:
            return False
        name = Path(relative).name
        parent = Path(relative).parent.name.lower()
        if parent == "picasso96" and name.lower() in EMULATOR_CARDS:
            self.note("removed", f"{relative} (an emulator's RTG driver)")
            return True
        if parent == "monitors" and name.lower() in EMULATOR_MONITORS:
            if self.rtg:
                #  Keep the bytes: the replacement monitor is the same loader.
                self.monitor_file = self._pending_data
                self.note("replaced", f"{relative} -> Devs/Monitors/{EMU68_BOARD}")
            else:
                self.note("removed", f"{relative} (this machine has no RTG)")
            return True
        if parent == "monitors" and name.lower().endswith(".info") \
                and name[:-len(".info")].lower() in EMULATOR_MONITORS:
            if self.rtg:
                self.monitor_icon = self._pending_data
            return True
        if not self.rtg and relative.replace("\\", "/").lower() in SCREENMODE_PREFS:
            self.note("removed", f"{relative} (a saved RTG screen mode would "
                                 f"open Workbench where you cannot see it)")
            return True
        return False

    def offer(self, relative: str, data: bytes) -> bytes:
        """Called with each file's contents; may rewrite it."""
        self._pending_data = data
        if not self.enabled:
            return data
        if Path(relative).parent.name.lower() == "picasso96":
            self._seen_picasso = True
        posix = relative.replace("\\", "/")
        if any(posix.lower() == f.lower() for f in STARTUP_FILES):
            return self._clean_startup(posix, data)
        return data

    def _clean_startup(self, relative: str, data: bytes) -> bytes:
        text = data.decode("latin-1")
        out: list[str] = []
        changed = 0
        for line in text.splitlines(keepends=True):
            stripped = line.strip().lower()
            command = stripped.lstrip(";").strip()
            if command and not stripped.startswith(";") and \
                    any(command.startswith(name) or f"/{name}" in command
                        for name in EMULATOR_COMMANDS):
                out.append("; [PiStorm] " + line)
                changed += 1
            else:
                out.append(line)
        if changed:
            self.note("edited", f"{relative}: commented out {changed} "
                                f"emulator-only command"
                                f"{'s' if changed != 1 else ''}")
        return "".join(out).encode("latin-1")

    # ----------------------------------------------------------- extra files

    def finish(self, target, progress: Progress) -> None:
        """Add the Emu68 RTG driver and its monitor icon to a filled volume."""
        if not self.enabled or not self._seen_picasso:
            if self.enabled and not self._seen_picasso:
                progress.log("  compatibility - no Picasso96 install found; "
                             "leaving graphics setup alone")
            return
        if not self.rtg:
            self.note("note", "no RTG display, so the emulator's graphics "
                              "driver was removed rather than replaced")
            return

        card = fetch_videocore_card(progress)
        if card is None:
            progress.log("  compatibility - WARNING: could not add "
                         f"{EMU68_CARD}; RTG will not work until it is "
                         "installed by hand")
            return

        libs = target.makedirs("Libs/Picasso96")
        target.write_file(libs, EMU68_CARD, card, check_existing=False)
        self.note("added", f"Libs/Picasso96/{EMU68_CARD} ({len(card)} bytes)")

        if self.monitor_file:
            monitors = target.makedirs("Devs/Monitors")
            target.write_file(monitors, EMU68_BOARD, self.monitor_file,
                              check_existing=False)
            if self.monitor_icon:
                try:
                    icon = amigainfo.set_tooltype(self.monitor_icon,
                                                  "BOARDTYPE", EMU68_BOARD)
                except Exception as error:  # noqa: BLE001 - an odd icon is not
                    #  worth failing a whole card for; copy it across unchanged
                    #  and say so, so the user can set BOARDTYPE by hand.
                    progress.log(f"  compatibility - could not retarget the "
                                 f"monitor icon: {error}")
                    icon = self.monitor_icon
                target.write_file(monitors, EMU68_BOARD + ".info", icon,
                                  check_existing=False)
                self.note("retargeted",
                          f"Devs/Monitors/{EMU68_BOARD}.info BOARDTYPE="
                          f"{EMU68_BOARD}")
        else:
            self.note("note", "no emulator monitor file was present, so no "
                              f"Devs/Monitors/{EMU68_BOARD} was created; add "
                              f"one with the Picasso96 installer")

    def summary(self) -> str:
        if not self.enabled:
            return "Compatibility fixes were switched off."
        if not self.fixes:
            return "No compatibility changes were needed."
        return (f"{len(self.fixes)} compatibility change"
                f"{'s' if len(self.fixes) != 1 else ''} applied.")
