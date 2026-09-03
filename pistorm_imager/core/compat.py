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
from collections.abc import Iterable
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

#  WHDLoad runs ExecuteStartup before every game and ExecuteCleanup after it.
#  An emulator installation puts its own tuning there - PiMiga sets the JIT
#  cache and CPU speed through uae-configuration - and on a PiStorm that
#  command does not exist, so every launch runs something that is not there.
#  Matched on the file name, not the path.  A tree copied whole gives
#  "S/WHDLoad.prefs", an overlay of that one file gives "S/WHDLoad.prefs" or
#  just "WHDLoad.prefs" depending on where it is going, and a rule that
#  insisted on one of those spellings silently did nothing for the others.
WHDLOAD_PREFS = ["whdload.prefs"]
#  iGame keeps an absolute path to every slave it knows about.
#  A forced display mode in WHDLoad's preferences.  Harmless under the
#  emulator a donor came from; fatal on a PiStorm, where forcing the mode
#  takes the machine down before the game is reached.
FORCED_MODES = {"pal", "ntsc"}

GAMES_LIST = "gameslist.csv"
#  iGame's list of drawers to scan, one AMIGA:path per line.  It is written
#  from what the donor system had, so it names collections that this card was
#  told to leave out.
GAMES_REPOS = "repos.prefs"
WHDLOAD_HOOKS = ("executestartup", "executecleanup")

#  A saved screen mode points at a specific display board.  Carried over to a
#  machine with no RTG, it opens Workbench on a screen that does not exist;
#  dropping it makes Workbench fall back to a native mode.
SCREENMODE_PREFS = ["prefs/env-archive/sys/screenmode.prefs",
                    "devs/env-archive/sys/screenmode.prefs"]

#  Monitor drivers for the Amiga's own chipset, best first.  AmigaOS ships
#  these in STORAGE:Monitors uninstalled; a system built for an emulator's RTG
#  board often has none of them in DEVS:Monitors, which leaves Prefs offering
#  no native screen mode at all on a machine whose video port is in use.
NATIVE_MONITORS = ["pal", "ntsc"]

#  Which output Workbench opens on comes down to one thing: whether AmigaOS
#  finds a saved screen mode in ENVARC:Sys.  With one there, Workbench opens on
#  the RTG board that mode names; with none, it falls back to a native mode.
#  That makes the choice a matter of moving one file, so it does not have to be
#  settled when the card is written - which matters, because whether the HDMI
#  monitor or the Amiga's own is switched on that day is not a property of the
#  card at all.
#
#  Nothing here is fabricated: the saved mode is whatever the system already
#  had, or whatever the user later saves in Prefs/ScreenMode.  Writing a screen
#  mode from scratch would mean guessing a Picasso96 display ID, and a wrong
#  guess opens Workbench on a screen that does not exist.
SWITCH_STORE = "Storage/PiStorm"
SWITCH_PREFS = "ScreenMode-RTG.prefs"
FIBF_SCRIPT = 0x40          # the "this is a script" protection bit

USE_HDMI_SCRIPT = """\
; Open Workbench on the RTG screen (the Pi's HDMI) from the next boot.
; Written by the PiStorm Imager.  Run it with:
;
;     Execute S:PiStorm-Use-HDMI
;
IF EXISTS SYS:{store}/{prefs}
  Copy >NIL: SYS:{store}/{prefs} TO ENVARC:Sys/screenmode.prefs
  Copy >NIL: SYS:{store}/{prefs} TO ENV:Sys/screenmode.prefs
  Echo "Workbench will open on the RTG screen after a reboot."
  Echo "Make sure the Pi's HDMI output has a monitor on it."
ELSE
  Echo "No RTG screen mode has been saved yet."
  Echo "Open Prefs/ScreenMode, pick a VideoCore mode and choose Save, then"
  Echo "run Execute S:PiStorm-Use-Amiga-Video to stash it."
ENDIF
"""

USE_NATIVE_SCRIPT = """\
; Open Workbench on the Amiga's own video output from the next boot.
; Written by the PiStorm Imager.  Run it with:
;
;     Execute S:PiStorm-Use-Amiga-Video
;
; The RTG screen mode is kept, so switching back to it loses nothing.
;
; Every step is guarded: in an AmigaDOS script a command that fails - deleting
; a file that is not there, making a drawer that already exists - stops the
; whole script at the default FAILAT of 10.
IF EXISTS ENVARC:Sys/screenmode.prefs
  IF NOT EXISTS SYS:{store}
    MakeDir SYS:{store}
  ENDIF
  Copy >NIL: ENVARC:Sys/screenmode.prefs TO SYS:{store}/{prefs}
  Delete >NIL: ENVARC:Sys/screenmode.prefs
ENDIF
IF EXISTS ENV:Sys/screenmode.prefs
  Delete >NIL: ENV:Sys/screenmode.prefs
ENDIF
Echo "Workbench will open on the Amiga's own video output after a reboot."
"""


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
                 rtg: bool = True, native: bool = False,
                 workbench_on_rtg: bool = True):
        self._pending_data: bytes = b""
        self.progress = progress
        self.enabled = enabled
        #  Whether the target is being watched on an RTG display at all.  With
        #  no RTG there is nothing to substitute the driver *for*, and the
        #  emulator's graphics setup has to come out rather than be replaced.
        self.rtg = rtg
        #  Whether the Amiga's own video output is also being watched.  Both
        #  can be true at once - RTG on the Pi's HDMI and native screens on a
        #  monitor plugged into the Amiga - and that is not the same as either
        #  one alone.
        self.native = native
        #  Which of the two Workbench itself should open on.
        self.workbench_on_rtg = rtg and (workbench_on_rtg or not native)
        self.fixes: list[Fix] = []
        #  Known-good copies of files, indexed by lower-case name, used when the
        #  source cannot be read.  A drive image served over a loop mount or a
        #  network share does occasionally throw a transient read error, and
        #  quietly omitting a library is far worse than saying so.
        self.spares: dict[str, Path] = {}
        #  Remembered so the replacement monitor can be built from the original.
        self.monitor_file: bytes | None = None
        self.monitor_icon: bytes | None = None
        #  Paths a chosen package is going to install a newer copy of. The
        #  file system here creates files and never overwrites them, so the
        #  first copy on the drive wins - which meant an imported drive's
        #  years-old WHDLoad beat the current release the user had ticked.
        #  Refusing it during the copy leaves the name free for the package.
        self._displace: set[str] = set()
        self._displaced: list[str] = []
        self._seen_picasso = False
        self._picasso_expected = False
        self._finished = False
        self._finish_classicwb = False
        self._classicwb_startup: bytes = b""
        #  Volume name -> (host folder it is filled from, paths left out), so
        #  a games list can be checked against what will actually be there.
        self.content: dict[str, tuple[Path, tuple[str, ...]]] = {}
        self._listing: dict[Path, dict[str, str]] = {}
        #  Monitor drivers already installed in DEVS:Monitors, and the spare
        #  native ones sitting in STORAGE:Monitors, so a native driver can be
        #  installed if the target needs one and the source has none.
        self._installed_monitors: set[str] = set()
        self._stored_monitors: dict[str, bytes] = {}
        #  A saved RTG screen mode that is being taken out of the way rather
        #  than thrown out, so switching back to it needs no rebuild.
        self._rtg_screenmode: bytes | None = None

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

    #  ClassicWB installs itself on the Amiga: it copies Commodore's files
    #  off a Workbench floppy, then puts the boot script it carries as
    #  T:Science in place of its installer's own. The files come from the
    #  Workbench disks here, so the rest can be done here too, and the card
    #  boots into the system instead of into an installer asking for a disk.
    CLASSICWB_REAL_STARTUP = "t/science"
    CLASSICWB_INSTALLER = ("s/startup-sequence", "s/activate", "t/science")

    def finish_classicwb_install(self) -> None:
        """Carry out on the card what its installer would do on the Amiga."""
        self._finish_classicwb = True

    def displace(self, paths: Iterable[str]) -> None:
        """Refuse these paths while copying, so a package can supply them.

        Only ever called with paths a package has already fetched, so a
        failed download cannot leave the card without the file it refused.
        """
        self._displace |= {p.replace("\\", "/").strip("/").lower()
                           for p in paths}

    def stop_displacing(self) -> None:
        """The copying is over; the packages may now write their own files.

        Displacing is a rule about *filling* a drive: it refuses a path so
        that the package which claimed it can have the name. The package's
        own files go on through the same pass, so leaving this switched on
        refused those too - and the file landed nowhere at all. Whole drawers
        were unaffected, which is what made it look like a packaging problem
        rather than this.
        """
        self._displace.clear()

    def skip(self, relative: str) -> bool:
        """True when a file should not be copied to the target at all."""
        #  Checked before `enabled`, because this is not a compatibility fix
        #  at all: it is the user's own choice of software, and it has to
        #  hold whether or not the compatibility pass is switched on.
        posix = relative.replace("\\", "/").strip("/").lower()
        if posix in self._displace:
            self._displaced.append(relative)
            self.note("replaced",
                      f"{relative} (the newer copy you chose is installed "
                      f"instead)")
            return True
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
        if self._finish_classicwb:
            here = relative.replace("\\", "/").lower()
            if here == self.CLASSICWB_REAL_STARTUP:
                #  Kept back and written out as the boot script below.
                self._classicwb_startup = self._pending_data
                return True
            if here in self.CLASSICWB_INSTALLER:
                self.note("removed", f"{relative} (its installer, carried out "
                                     f"here instead)")
                return True
        if Path(relative).name.lower() == GAMES_LIST:
            #  iGame's list holds an absolute path to every slave, written on
            #  somebody else's machine. Editing it to match this card was
            #  half-guessing at another program's data; iGame builds the list
            #  itself from what is actually here, which cannot be wrong.
            self.note("removed",
                      f"{relative} (iGame builds its own from the card - "
                      f"Actions > Scan Repositories, once)")
            return True
        if not self.workbench_on_rtg \
                and relative.replace("\\", "/").lower() in SCREENMODE_PREFS:
            if self.rtg:
                #  RTG is still in use, so this mode is worth keeping: it is
                #  stashed rather than dropped, and the switcher puts it back.
                self._rtg_screenmode = self._pending_data
                self.note("moved", f"{relative} -> {SWITCH_STORE}/"
                                   f"{SWITCH_PREFS} (Workbench is to open on "
                                   f"the Amiga's own video output)")
            else:
                self.note("removed", f"{relative} (a saved RTG screen mode "
                                     f"would open Workbench where you cannot "
                                     f"see it)")
            return True
        return False

    def offer(self, relative: str, data: bytes) -> bytes:
        """Called with each file's contents; may rewrite it."""
        self._pending_data = data
        if not self.enabled:
            return data
        parts = relative.replace("\\", "/").lower().split("/")
        if len(parts) >= 2 and parts[-2] == "monitors":
            name = parts[-1]
            if len(parts) >= 3 and parts[-3] == "devs" \
                    and not name.endswith(".info"):
                self._installed_monitors.add(name)
            elif len(parts) >= 3 and parts[-3] == "storage" \
                    and name.removesuffix(".info") in NATIVE_MONITORS:
                self._stored_monitors[name] = data
        if relative.replace("\\", "/").lower().startswith("libs/picasso96/"):
            self._seen_picasso = True
        posix = relative.replace("\\", "/")
        if any(posix.lower() == f.lower() for f in STARTUP_FILES):
            return self._clean_startup(posix, data)
        name = Path(posix).name.lower()
        if name in WHDLOAD_PREFS:
            return self._clean_whdload_prefs(posix, data)
        if name == GAMES_REPOS:
            return self._filter_repositories(posix, data)
        return data

    # -------------------------------------------------- iGame's games list

    def _resolve(self, root: Path, relative: str) -> bool:
        """Whether ``relative`` exists under ``root``, ignoring case.

        The list was written on a case-insensitive Amiga volume and is being
        checked against a Linux tree, where "WHDLoad" and "WHDLOAD" are two
        different directories.  Listings are cached because a games list runs
        to thousands of lines through the same few drawers.
        """
        here = root
        for part in [p for p in relative.split("/") if p]:
            names = self._listing.get(here)
            if names is None:
                try:
                    names = {entry.name.lower(): entry.name
                             for entry in here.iterdir()}
                except OSError:
                    names = {}
                self._listing[here] = names
            actual = names.get(part.lower())
            if actual is None:
                return False
            here = here / actual
        return True

    def _on_the_card(self, amiga_path: str) -> bool | None:
        """Whether an AMIGA:path/file will be on the finished card.

        None when it cannot be judged - a volume nothing here fills - in which
        case the entry is kept, because dropping what we cannot check would
        be worse than leaving it.
        """
        volume, _, rest = amiga_path.partition(":")
        known = self.content.get(volume.strip().upper())
        if known is None or not rest:
            return None
        root, excludes = known
        lowered = rest.lower().lstrip("/")
        for skip in excludes:
            skip = skip.replace("\\", "/").strip("/").lower()
            if skip and (lowered == skip or lowered.startswith(skip + "/")):
                return False
        return self._resolve(root, rest)

    def _filter_repositories(self, relative: str, data: bytes) -> bytes:
        """Drop the drawers iGame is told to scan that will not be there.

        Filtering the games list was only half of it.  iGame also keeps the
        list of drawers it scans, and that still named every collection the
        donor had - including the ones this card was told to leave out.  So a
        card with the AGA games excluded still sent iGame looking through
        Games:WHDLOAD/AGA/, which does not exist on it.
        """
        if not self.content:
            return data
        out: list[str] = []
        dropped: list[str] = []
        for line in data.decode("latin-1").splitlines(keepends=True):
            path = line.strip()
            if not path or path.startswith(";"):
                out.append(line)
                continue
            if self._on_the_card(path) is False:
                dropped.append(path)
                continue
            out.append(line)
        if dropped:
            self.note("edited",
                      f"{relative}: dropped {len(dropped)} repository that "
                      f"will not be on the card ("
                      + ", ".join(sorted(dropped)) + ")")
        return "".join(out).encode("latin-1")

    def _clean_whdload_prefs(self, relative: str, data: bytes) -> bytes:
        """Take out what an emulator put in this file and a PiStorm cannot use.

        Two things.  The hooks that shell out to the emulator's own control
        program, which a PiStorm has not got - and the forced display mode.

        The second was found the hard way.  A donor's preferences carry
        "PAL", which asks WHDLoad to force that mode before handing over, and
        on a PiStorm every single game died on the spot: a yellow screen -
        a CPU exception with no operating system left to draw a Guru - then
        black, then nothing.  Not one game ran, on any card this tool has
        ever built.

        It took bisecting a card down to Workbench and WHDLoad alone to find
        it, because everything else pointed elsewhere: the same game runs off
        Commodore's own floppy, the files on the card are byte-identical to
        the source, and it fails the same way on FFS and PFS3, on a 68020 and
        a 68040.  The one difference that survived every elimination was this
        file, and within it this line.

        Left out, WHDLoad uses the mode the machine is already in, which is
        what it does on a bare install and is right on hardware whose display
        the user has already chosen.
        """
        out: list[str] = []
        hooks = modes = 0
        for line in data.decode("latin-1").splitlines(keepends=True):
            stripped = line.strip().lower()
            key, _, value = stripped.partition("=")
            if stripped.startswith(";"):
                out.append(line)
            elif (key.strip() in WHDLOAD_HOOKS
                    and any(name in value for name in EMULATOR_COMMANDS)):
                out.append(";" + line)
                hooks += 1
            elif stripped.split(";")[0].strip() in FORCED_MODES:
                out.append(";" + line)
                modes += 1
            else:
                out.append(line)
        if hooks:
            self.note("edited",
                      f"{relative}: commented out {hooks} WHDLoad hook"
                      f"{'s' if hooks != 1 else ''} that call an emulator's "
                      f"control program, which a PiStorm has not got")
        if modes:
            self.note("edited",
                      f"{relative}: stopped WHDLoad forcing a display mode, "
                      f"which kills every game on a PiStorm before it starts")
        return "".join(out).encode("latin-1")

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

    #  A volume is filled from many trees - the floppies, then a package at a
    #  time - and this used to run after each of them.  It decided what to do
    #  about the graphics driver after the first, which on a card with any
    #  software on it is WHDLoad, several overlays before Picasso96 arrives:
    #  so a card that had Picasso96 installed was told it had none.  The
    #  builder calls it once now, on the system volume, when it is full.
    finish_with_each_tree = False

    def finish(self, target, progress: Progress) -> None:
        """Add whatever drivers the target's displays need to a filled volume.

        Called once, by the builder, when the volume is full. It guards itself
        as well, because everything here writes a file and writing the same
        script twice is not harmless - it ends the build.
        """
        if self._finished:
            return
        self._finished = True
        if self._classicwb_startup:
            folder = target.makedirs("S")
            target.write_file(folder, "Startup-Sequence",
                              self._classicwb_startup, check_existing=False)
            self.note("added", "S/Startup-Sequence from the one the "
                               "distribution carries, so the card boots into "
                               "the system rather than into its installer")
        if self.enabled and self.native:
            self._install_native_monitor(target)
        if self.enabled and self.rtg and self.native:
            self._install_display_switch(target)
        self._finish_rtg(target, progress)

    def _install_display_switch(self, target) -> None:
        """Let the display be changed on the Amiga, without rebuilding the card.

        Which monitor is switched on today is not something the card can know,
        so with both outputs wired the answer is not fixed at build time: two
        scripts move the saved screen mode in and out of ENVARC:Sys, which is
        the whole of what decides where Workbench opens.
        """
        if self._rtg_screenmode is not None:
            store = target.makedirs(SWITCH_STORE)
            target.write_file(store, SWITCH_PREFS, self._rtg_screenmode,
                              check_existing=False)
            self.note("added", f"{SWITCH_STORE}/{SWITCH_PREFS} (the RTG screen "
                               f"mode, kept so it can be switched back on)")
        scripts = target.makedirs("S")
        for name, text in (("PiStorm-Use-HDMI", USE_HDMI_SCRIPT),
                           ("PiStorm-Use-Amiga-Video", USE_NATIVE_SCRIPT)):
            body = text.format(store=SWITCH_STORE, prefs=SWITCH_PREFS)
            target.write_file(scripts, name, body.encode("latin-1"),
                              protect=FIBF_SCRIPT, check_existing=True)
        self.note("added", "S/PiStorm-Use-HDMI and S/PiStorm-Use-Amiga-Video "
                           "(move Workbench between the two outputs, then "
                           "reboot)")

    def _install_native_monitor(self, target) -> None:
        """Make sure native screen modes can be chosen at all.

        A system built around an emulator's RTG board frequently has nothing in
        DEVS:Monitors but that board.  On a machine whose own video output is
        being watched that leaves Prefs with no native mode to offer, so the
        uninstalled copy AmigaOS ships in STORAGE:Monitors is installed.
        """
        if any(name in NATIVE_MONITORS for name in self._installed_monitors):
            return
        pick = next((n for n in NATIVE_MONITORS if n in self._stored_monitors),
                    None)
        if pick is None:
            if self._stored_monitors or self._installed_monitors:
                self.note("note", "no native monitor driver was available to "
                                  "install; Workbench will use the default "
                                  "screen mode only")
            return
        monitors = target.makedirs("Devs/Monitors")
        proper = pick.upper() if pick in ("pal", "ntsc") else pick.capitalize()
        target.write_file(monitors, proper, self._stored_monitors[pick],
                          check_existing=False)
        icon = self._stored_monitors.get(pick + ".info")
        if icon is not None:
            target.write_file(monitors, proper + ".info", icon,
                              check_existing=False)
        self.note("added", f"Devs/Monitors/{proper} (from Storage, so native "
                           f"screen modes can be chosen)")

    def expect_picasso(self) -> None:
        """Say that a package will install Picasso96, whatever the copy sees.

        Recognising it by path only works when it arrives inside a tree that
        names it - importing somebody's whole System drive does. A package
        overlay is a plain directory copied *to* Libs/Picasso96, and the paths
        offered here are relative to the source, so nothing in them says
        Picasso96 at all: a card that had it installed was told it had none,
        and went out without the graphics driver a PiStorm needs.
        """
        self._picasso_expected = True

    @property
    def _picasso_installed(self) -> bool:
        return self._seen_picasso or self._picasso_expected

    def _finish_rtg(self, target, progress: Progress) -> None:
        if not self.enabled or not self._picasso_installed:
            if self.enabled:
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
