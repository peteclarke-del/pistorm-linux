"""Optional software to add to a Workbench installed from floppy images.

A Workbench built from the original disks is exactly what shipped in 1994: no
archiver, no installer, and no idea what WHDLoad is. The pieces almost everyone
adds next are listed here.

Each package can arrive by either of two routes:

* **From a donor system you already have.** An emulator installation such as
  PiMiga carries most of them, and so does any Workbench you have already set
  up; point at its System drive and whatever is there becomes available. This
  is the only route for anything that is not freely distributable - IBrowse and
  MiamiDx among them - which is why they are offered but never downloaded.
* **From Aminet.** Freely distributable packages are fetched over the network
  and cached under ``~/.cache/pistorm-imager/packages``, so a second card costs
  no download at all.

Not everything can be installed by copying files. VisualPrefs, MCP, NewIcons
and ToolsDaemon patch the ROM or the Workbench binaries and ship an Amiga
``Installer`` script to do it; there is no honest way to apply those from
Linux. Those packages are unpacked into a drawer on the card instead, ready to
be installed on the Amiga itself, and say so in their description.

Which packages make sense depends on the machine. An OCS A500 looking at its
own 15 kHz video wants FBlit and a tight palette; an AGA machine on the Pi's
HDMI wants Picasso96 and can afford Scalos. :func:`suggested` puts that
judgement in one place.
"""
from __future__ import annotations

import dataclasses
import enum
import re
import shutil
import subprocess
import urllib.request
from collections.abc import Iterable
from pathlib import Path

from .machines import Chipset, Display, Machine
from .util import Progress, human_size

AMINET = "https://aminet.net/"
USER_AGENT = "pistorm-imager"


class Category(enum.Enum):
    """How the packages are grouped when they are offered."""

    SYSTEM = "System"
    UPDATES = "Updates and patches"
    LOOK = "Look and feel"
    SPEED = "Speed"
    NETWORK = "Networking"
    MEDIA = "Music and pictures"
    EXTRAS = "Handy extras"


#  Drawers an archive may carry that belong somewhere definite on the card.
#  A ``merge`` download is laid out like a Workbench disk, so its contents can
#  be placed without naming every file - which matters for an archive this
#  code cannot fetch and therefore cannot read the layout of in advance.
SYSTEM_DRAWERS = ("C", "L", "S", "Libs", "Devs", "Prefs", "Locale", "Rexxc",
                  "Classes", "Fonts", "Storage", "System", "Tools",
                  "Utilities", "Expansion")


@dataclasses.dataclass(frozen=True)
class Download:
    """A freely distributable archive, from Aminet or a named source.

    ``items`` maps paths inside the unpacked archive to destinations on the
    card. When it is empty the whole archive is unpacked into ``stage``
    instead, because the package installs itself with its own script.
    """

    path: str
    items: tuple[tuple[str, str], ...] = ()
    stage: str = ""
    #  Some Aminet uploads are self-extracting Amiga executables rather than
    #  archives; nothing here can unpack one, so the file itself goes on the
    #  card to be run there.
    raw: bool = False
    #  Lay the archive out over the card by drawer name rather than by a list
    #  of files: everything in its C, Libs, Devs and S goes to the card's.
    merge: bool = False
    #  Where a person can fetch the archive by hand.  Some publishers put
    #  their downloads behind a page that will not serve them to anything but
    #  a browser, and then the cached copy is the only route.
    source: str = ""
    #  True when there is no URL that can be fetched without a browser. The
    #  build then uses a copy the user has put in the cache, and says where
    #  to get one when there is none, rather than downloading a login page
    #  and caching it as though it were the archive.
    manual: bool = False
    #  Paths inside the archive that must not be placed, because they are
    #  meant to be merged into a file the card already has rather than to
    #  replace it.
    skip: tuple[str, ...] = ()
    #  Files this tool writes itself, as (name, destination, text). An
    #  archive that ships templates for other people's hardware still needs
    #  one for the machine being built.
    write: tuple[tuple[str, str, str], ...] = ()
    #  (path inside the archive, destination, name on the card). For an
    #  archive that ships one binary per processor: the card wants the one
    #  its machine has, under the name the icon launches.
    rename: tuple[tuple[str, str, str], ...] = ()

    @property
    def url(self) -> str:
        if self.path.startswith(("http://", "https://")):
            return self.path
        return AMINET + self.path

    @property
    def where(self) -> str:
        """Where the archive is published, for saying so in the log."""
        if self.manual or self.source:
            return self.source or "its publisher"
        return "Aminet" if not self.path.startswith("http") else self.path
    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclasses.dataclass(frozen=True)
class Package:
    key: str
    label: str
    description: str
    category: Category = Category.SYSTEM
    #  Where it comes from. Every package has one: software used to be able to
    #  come out of a donor system instead, which meant a card was built from
    #  whatever some other installation happened to hold.
    download: Download | None = None
    default: bool = False
    #  Only worth having where the Pi's HDMI is driving an RTG screen.
    rtg_only: bool = False
    #  Not optional wherever it does suit the setup: without it the thing the
    #  user asked for does not work at all. An RTG display with no Picasso96
    #  has no RTG screen modes, so the HDMI output shows nothing.
    essential: bool = False
    #  Chipsets this makes sense on; empty means any.
    chipsets: tuple[Chipset, ...] = ()
    #  Waive the chipset restriction when there is an RTG screen: some of this
    #  is really about how much display there is, not which chips draw it.
    or_rtg: bool = False
    #  Only worth having where the Amiga's own video is actually being watched.
    native_only: bool = False
    #  Lines to add to S:User-Startup.  Copying a file into LIBS: is often not
    #  enough: Workbench 3.1 keeps using the icon.library in ROM unless
    #  something soft-kicks the one on disk over it, and a patch like FBlit
    #  does nothing until it is run.
    startup: tuple[str, ...] = ()
    #  Other packages this one cannot run without.  iGame, AmFTP, NetSurf and
    #  WookieChat are all MUI applications: copied on their own they land on
    #  the card, appear on Workbench and then do nothing at all when clicked,
    #  because muimaster.library is not there.  A dependency is pulled in
    #  whether or not the user thought to tick it.
    requires: tuple[str, ...] = ()
    note: str = ""
    #  What job this does, where two packages doing it are alternatives
    #  rather than companions. Ticking one while the other is on is worth a
    #  question - it is rarely what anybody means, and the two patch the same
    #  part of the system. Left empty for the great majority, which coexist
    #  perfectly well: three module players on one card is a preference, not
    #  a conflict.
    role: str = ""
    #  True when nobody would choose this for its own sake - it is here to
    #  satisfy something else. Such a package goes away with the last thing
    #  that needed it; one that is useful on its own stays, because turning
    #  off a browser should not take MUI away from everything else.
    support_only: bool = False
    #  Drawers on a prepared drive that hold an older copy of this very
    #  program, under a name or in a place this build would never write. A
    #  distribution can carry its own: ClassicWB FULL keeps SysInfo 3.24 from
    #  1993 in Tools/SysInfo while this installs 4.4 into Utilities/SysInfo,
    #  so both land on the card and only one is ever opened.
    #
    #  Each entry is taken out **whole**, so each one has to be a drawer that
    #  holds this program and nothing else - checked against a real
    #  distribution, never inferred from a name. ClassicWB's System/FBlit
    #  looks like a duplicate and is not: it carries the same FBlit build as
    #  the package plus FBlitGUI, which the package does not ship, so
    #  removing it would take a program away.
    supersedes: tuple[str, ...] = ()

    @property
    def manual(self) -> bool:
        """Whether this one has to finish installing on the Amiga itself."""
        return bool(self.download and not self.download.items
                    and not self.download.merge)

    @property
    def downloadable(self) -> bool:
        return self.download is not None

    def suits(self, chipset: Chipset, display: Display) -> bool:
        if self.rtg_only and not display.uses_rtg:
            return False
        if self.native_only and not display.uses_native:
            return False
        if self.chipsets and chipset not in self.chipsets:
            if not (self.or_rtg and display.uses_rtg):
                return False
        return True


STAGING = "Storage/Install"          # where self-installing packages land


CATALOGUE: list[Package] = [
    # ----------------------------------------------------------- system
    Package(
        "whdload", "WHDLoad",
        "Runs floppy games and demos from the hard drive. Almost every game "
        "collection is built around it.",
        #  MMULib and a newer SetPatch were once required here, on the
        #  reasoning that a 68040 needs modern CPU support. Tested, the
        #  opposite is true: either of them stops every WHDLoad game dead.
        #  Aminet's dev/misc/WHDLoad_usr.lha is a 2007 upload of 16.8 and has
        #  not moved since; the author's own site serves the current release.
        #  A card built from Aminet came out older than the ready-made
        #  distributions it was competing with.
        download=Download("https://whdload.de/whdload/WHDLoad_usr.lha",
                          (("WHDLoad/C/WHDLoad", "C"),
                           ("WHDLoad/C/WHDLoadCD32", "C"),
                           ("WHDLoad/C/Patcher", "C")),
                          source="whdload.de"),
        default=True,
        #  Nearly every slave asks WHDLoad for the Kickstart the game expects
        #  and will not start without it. Those are Commodore ROM images:
        #  nobody publishes them, and they used to be copied out of a donor
        #  system. With no donor there is nowhere honest to get them, so the
        #  card says what is missing rather than launching a game and falling
        #  over on the spot.
        note="Games that need a Kickstart image want them in Devs/Kickstarts "
             "on the card - they are Commodore's and cannot be fetched, so "
             "copy your own there afterwards.",
    ),
    Package(
        "lha", "LhA",
        "The archiver Amiga software is distributed in. Without it very little "
        "downloaded from Aminet can be unpacked.",
        #  Aminet ships LhA as a self-extracting Amiga program - which is
        #  what an archiver has to be, since you need one to unpack the
        #  other. The archive inside it is an ordinary LhA one, so it is
        #  taken out here and the right build installed, rather than leaving
        #  the card with no archiver until somebody runs the extractor.
        download=Download("util/arc/lha.run",
                          rename=(("lha_68040", "C", "LhA"),)),
        default=True,
        note="The 68040 build, which is what Emu68 provides.",
    ),
    Package(
        "installer", "Installer",
        "Commodore's installer, which most third-party install scripts expect "
        "to find and fail without.",
        download=Download("util/misc/Installer-43_3.lha",
                          (("Installer43_3/Installer", "C"),)),
        default=True,
    ),
    Package(
        "newinstaller", "NewInstaller",
        "Makes the Commodore Installer's script windows look like something "
        "from this century, and can stand in for it entirely. Installer "
        "scripts that other software ships then run through this instead.",
        category=Category.SYSTEM,
        #  Its own Install script copies the program into C: and its
        #  libraries with copylib, which is what these two lines do. The
        #  rest - its demos, its documentation, the tool that sets a theme -
        #  is staged, because choosing a theme is a decision and this cannot
        #  make it.
        download=Download(
            "util/wb/NewInstaller17.lha",
            (("NewInstaller1_7/NewInstaller", "C"),
             ("NewInstaller1_7/Libs", "Libs"),
             ("NewInstaller1_7/Catalogs", "Locale/Catalogs"),
             ("NewInstaller1_7/Defaults", STAGING + "/NewInstaller/Defaults"),
             ("NewInstaller1_7/Tools", STAGING + "/NewInstaller/Tools"),
             ("NewInstaller1_7/Docs", STAGING + "/NewInstaller/Docs"))),
        note="Installed as C:NewInstaller. To have it replace the Commodore "
             "Installer outright, run its own Install from Storage/Install "
             "on the Amiga - it asks questions this cannot answer for you.",
    ),
    Package(
        "kingcon", "KingCON",
        "A console handler with a command history, filename completion and "
        "an editable command line - what the Shell should always have been. "
        "The handler goes into L: and the mountfile into DEVS:DOSDrivers.",
        category=Category.EXTRAS,
        download=Download(
            "util/shell/KingCON_1.3.lha",
            (("KingCON1.3/Handler/KingCON-handler.020", "L"),
             ("KingCON1.3/Docs", STAGING + "/KingCON/Docs"),
             ("KingCON1.3/Installation", STAGING + "/KingCON"))),
        note="The 68020 build of the handler is installed. Its own "
             "Installation script, in Storage/Install, mounts it as the "
             "console and can make it the default - which changes how every "
             "Shell on the card behaves, so it is left for you to decide.",
    ),
    Package(
        "blazewcp", "BlazeWCP",
        "A 32-bit chunky-to-planar patch for the OS chunky functions, which "
        "is what anything drawing a chunky picture on a planar screen goes "
        "through.",
        category=Category.SPEED,
        download=Download("util/boot/BlazeWCP178.lha",
                          (("BlazeWCP", "C"),
                           ("BlazeWCP.guide", STAGING + "/BlazeWCP"))),
        startup=("IF EXISTS C:BlazeWCP",
                 "   C:BlazeWCP >NIL:",
                 "EndIF"),
        note="Started from S:User-Startup. It patches the operating "
             "system's chunky drawing, so if anything draws oddly, take that "
             "line out and reboot.",
    ),
    Package(
        "mmulib", "68k CPU libraries (MMULib)",
        "Modern replacements for the CPU support libraries. Workbench 3.1 "
        "ships 68040.library 37.30 from 1994; these are maintained, and a "
        "PiStorm is a 68040-class machine that depends on them.",
        category=Category.UPDATES,
        #  Thomas Richter's MMULib, freely distributable from Aminet.
        #
        #  NOT on by default, and not to be taken lightly: with these
        #  installed, every WHDLoad game dies the moment it is launched -
        #  a yellow screen, which is a CPU exception with no operating
        #  system left to draw a Guru, then nothing.  Proven by building
        #  the same card with and without them and running the same game.
        download=Download("util/libs/MMULib.lha",
                          (("MMULib/Libs", "Libs"),)),
        note="Do not install this on a card for games: it stops every "
             "WHDLoad title from starting. Worth having on a machine used "
             "for applications, where the newer CPU support is the point.",
    ),
    Package(
        "mui", "MUI",
        "Magic User Interface: the toolkit a great deal of Amiga software "
        "draws itself with. Nothing that needs it will start without it.",
        #  MUI is not a drawer of files that can be scattered into LIBS: - it
        #  expects to be found through a MUI: assign, with its own libraries
        #  and locale added to the system's.  This is how a real MUI install
        #  is arranged, and how the donor systems carry it.
        #  MUI 3.8 as published. A donor's MUI is usually richer - PiMiga's
        #  carries 84 classes against this archive's 36 - so the release goes
        #  on first and the donor fills in the extra classes behind it.
        download=Download("util/libs/mui38usr.lha",
                          (("MUI", "System/MUI"),)),
        #  MUI reads its configuration from ENV:MUI, which Workbench fills
        #  from ENVARC: at boot.  Without it every MUI application starts on
        #  built-in defaults and loses whatever the donor had set up.
        #  MUI's own key is picked up by the key rule in
        #  resolve_dependencies, which matches it to this drawer's name.
        startup=(
            "IF EXISTS SYS:System/MUI",
            "   Assign >NIL: MUI: SYS:System/MUI",
            "   IF EXISTS MUI:Libs",
            "      Assign >NIL: ADD LIBS: MUI:Libs",
            "   EndIF",
            "   IF EXISTS MUI:Locale",
            "      Assign >NIL: ADD LOCALE: MUI:Locale",
            "   EndIF",
            "EndIF",
        ),
    ),
    Package(
        "mcc_nlist", "MUI NList classes",
        "The list classes a great deal of MUI software is built on - iGame's "
        "games list among them. Not part of MUI itself.",
        category=Category.SYSTEM,
        download=Download(
            "dev/mui/MCC_NList-0.128.lha",
            (("MCC_NList/Libs/MUI/AmigaOS3", "System/MUI/Libs/mui"),)),
        requires=("mui",),
        support_only=True,
    ),
    Package(
        "mcc_texteditor", "MUI TextEditor class",
        "The editable text class MUI software uses for anything longer than "
        "a line. Not part of MUI itself.",
        category=Category.SYSTEM,
        download=Download(
            "dev/mui/MCC_TextEditor-15.56.lha",
            (("MCC_TextEditor/Libs/MUI/AmigaOS3", "System/MUI/Libs/mui"),)),
        requires=("mui",),
        support_only=True,
    ),
    Package(
        "mcc_urltext", "MUI UrlText class",
        "Draws a clickable web address inside a MUI window. iGame lists it "
        "as optional; without it the window still opens.",
        category=Category.SYSTEM,
        download=Download("dev/mui/MCC_Urltext.lha",
                          (("MCC_Urltext/MUI/Urltext.mcc",
                            "System/MUI/Libs/mui"),)),
        requires=("mui",),
        support_only=True,
    ),
    Package(
        "igame", "iGame",
        "A launcher that lists WHDLoad games with their screenshots.",
        #  Nothing from a donor. A donor's copy is whatever its author
        #  installed - PiMiga's is v2.1 from 2022 - and it arrives with that
        #  person's games list, their screenshots and their settings, all
        #  written against their machine. The release from Aminet is the whole
        #  package and starts empty, which is what a program that scans your
        #  own drives should do.
        download=Download(
            "util/misc/iGame.lha",
            items=(("iGame-v2.6.1", "Programs/iGame"),),
            #  One binary per processor is shipped; Emu68 gives a PiStorm a
            #  68040, and the icon launches whatever is called "iGame".
            rename=(("iGame-v2.6.1/iGame.040", "Programs/iGame", "iGame"),),
            #  guigfx.library and render.library draw its screenshots, and
            #  both are compiled for a processor with an FPU: render.library
            #  alone carries 153 floating point instructions, and no build
            #  without them exists. Emu68 gives a PiStorm a 68040 with no
            #  FPU, so calling one is a line-F exception - which is the guru
            #  8000000B that iGame's own site warns about. They are optional,
            #  so the card does without them and says so here.
            write=(("igame.prefs", "Programs/iGame",
                    "no_guigfx=1\n"
                    "filter_use_enter=0\n"
                    "hide_side_panel=0\n"
                    "start_with_favorites=0\n"
                    "save_stats_on_exit=0\n"
                    "no_smart_spaces=0\n"
                    "titles_from_dirs=1\n"
                    "hide_screenshots=1\n"
                    "screenshot_width=320\n"
                    "screenshot_height=256\n"),)),
        #  Its window is built from MUI classes that MUI itself does not
        #  carry, so a card with no donor still has everything it opens.
        requires=("mui", "mcc_nlist", "mcc_texteditor", "mcc_urltext"),
    ),
    Package(
        "identify", "identify.library",
        "Lets tools name the hardware they are running on. A dependency of "
        "several of the others.",
        download=Download("util/libs/Identify.lha",
                          (("Identify/libs/identify.library", "Libs"),)),
    ),
    Package(
        "copyicon", "CopyIcon",
        "Copies an icon's image onto another file, which is how a hand-made "
        "icon set gets applied.",
        download=Download("util/wb/CopyIcon44.lha",
                          (("CopyIcon44/CopyIcon", "C"),)),
    ),
    Package(
        "mcp", "MCP",
        "Master Control Program: a large collection of system patches and "
        "commodities. Patches the system, so it installs itself on the Amiga.",
        download=Download("util/cdity/MCP130.lha", stage=STAGING + "/MCP"),
        note="Run its Installer from Storage/Install on the Amiga.",
    ),
    Package(
        "toolsdaemon", "ToolsDaemon",
        "Adds your own entries to the Workbench Tools menu. Patches Workbench, "
        "so it installs itself on the Amiga.",
        download=Download("util/boot/ToolsDaemon22.lha",
                          stage=STAGING + "/ToolsDaemon"),
        note="Run its patch script from Storage/Install on the Amiga.",
    ),

    # ------------------------------------------------------ look and feel
    Package(
        "iconlib", "PeterK's icon.library",
        "A far faster icon.library that also understands modern icon formats. "
        "Worth having on any machine, and the PiStorm renders them instantly.",
        category=Category.LOOK,
        #  Taken from a donor in preference, because a working system already
        #  has the pieces this needs arranged together: LoadModule to install
        #  it, and the workbench.library that goes with it.
        download=Download("util/libs/IconLib_46.4.lha",
                          (("IconLib_46.4/Libs/icon.library", "Libs"),
                           ("IconLib_46.4/ThirdParty/LoadResident/LoadResident",
                            "C"))),
        #  Nothing in S:User-Startup: see StartupSequenceEditor.  By the time
        #  that file runs, IPrefs has opened the ROM icon.library, and a
        #  library already in the system list cannot be replaced - the Amiga
        #  was asked and answered 40.1 with 51.4 sitting unused in LIBS:.
        note="Installed by LoadModule at the top of S:Startup-Sequence, which "
             "is the only point early enough to replace the one in ROM.",
    ),
    Package(
        "magicmenu", "MagicMenu",
        "Turns the menu bar into a pop-up menu under the pointer, instead of a "
        "trip to the top of the screen.",
        category=Category.LOOK,
        download=Download("util/wb/MagicMenu_3.1.lha",
                          #  The icon comes from a drawer of its own, and
                          #  without one Workbench never starts the program.
                          #  Of the two sets the archive ships, DualPNG is a
                          #  PNG file with an .info name - an OS4 icon, which
                          #  Workbench 3.1 cannot read - so the classic one
                          #  is the only one that works here. It carries
                          #  DONOTWAIT and STARTPRI=80 already.
                          (("MagicMenu/WBStartup/MagicMenu", "WBStartup"),
                           ("MagicMenu/Icons/MagicWB/MagicMenu.info",
                            "WBStartup"))),
    ),
    Package(
        "visualprefs", "VisualPrefs",
        "Redraws window borders and gadgets - thin, flat, modern instead of "
        "the stock bevels. Patches the GUI, so it installs itself on the Amiga.",
        category=Category.LOOK,
        download=Download("util/wb/VisualPrefs.lha",
                          stage=STAGING + "/VisualPrefs"),
        note="Run its Installer from Storage/Install on the Amiga.",
    ),
    Package(
        "fullpalette", "FullPalette",
        "Locks down the Workbench palette so that opening a program cannot "
        "scramble your desktop colours. Matters most on a native screen.",
        category=Category.LOOK,
        native_only=True,
        download=Download("util/wb/FullPalette22.lha",
                          (("FullPalette/FullPalette", "WBStartup"),
                           ("FullPalette/FullPalette.info", "WBStartup"),
                           ("FullPalette/FPPrefs", "Prefs"))),
    ),
    Package(
        "newicons", "NewIcons",
        "Icons that redraw themselves in whatever palette the screen has. "
        "Patches the system, so it installs itself on the Amiga.",
        category=Category.LOOK,
        download=Download("util/wb/NewIcons46.lha", stage=STAGING + "/NewIcons"),
        note="Run its Installer from Storage/Install on the Amiga.",
        role="default icons",
    ),
    Package(
        "birdie", "Birdie",
        "Patterns in the window borders, which is most of what makes a "
        "Workbench look like somebody's rather than the factory's.",
        category=Category.LOOK,
        download=Download("util/wb/birdie2000.lha",
                          (("Birdie", "C"),
                           ("Patterns", "Prefs/Presets/Birdie"))),
        #  Its own documentation gives this line, and says it has to come
        #  after IPrefs - which is where package startup lines go anyway.
        startup=("C:Run >NIL: C:Birdie",),
        note="Installed into C: with its patterns in Prefs/Presets/Birdie, "
             "and started from S:User-Startup.",
    ),
    Package(
        "powerwindows", "PowerWindows",
        "Reshapes the window gadgets and borders, and can render icons the "
        "way later systems do.",
        category=Category.LOOK,
        #  It carries its own external routines and images and looks for them
        #  beside itself, so the drawer goes on whole rather than the one
        #  binary being lifted out of it.
        download=Download("util/misc/PowerWindows.lha",
                          stage="Utilities/PowerWindows"),
        note="Installed into Utilities/PowerWindows. Drag PowerWindows into "
             "WBStartup on the Amiga to have it run at every boot.",
    ),
    Package(
        "deficons", "DefIcons",
        "Gives every file an icon chosen from what it actually is. Without it "
        "a Workbench window shows programs and nothing else, which is most of "
        "why a stock 3.1 desktop looks so bare.",
        category=Category.LOOK,
        download=Download("util/wb/DefIcons44.lha",
                          (("DefIcons44/DefIcons44", "WBStartup"),
                           ("DefIcons44/DefIcons44.info", "WBStartup"),
                           ("DefIcons44/DefIconsPrefs", "Prefs"),
                           ("DefIcons44/deficons.prefs",
                            "Prefs/Env-Archive"))),
        default=True,
        role="default icons",
    ),
    Package(
        "freewheel", "FreeWheel",
        "Mouse wheel support: scrolls the window under the pointer. Its "
        "absence is one of the first things anyone notices.",
        category=Category.LOOK,
        #  The 020 build, since a PiStorm is a 68040 - but installed under
        #  the plain name. A drive that brings its own FreeWheel keeps it in
        #  WBStartup under exactly that name, and leaving ours as
        #  "FreeWheel_020" put a second copy of the same commodity beside it
        #  rather than in place of it: two input handlers scrolling one
        #  window. Under the same name it displaces the older copy, which is
        #  what choosing the package asked for.
        download=Download("util/mouse/FreeWheel.lha",
                          (("FreeWheel/FreeWheel.cfg", "S"),),
                          rename=(("FreeWheel/FreeWheel_020", "WBStartup",
                                   "FreeWheel"),
                                  ("FreeWheel/FreeWheel_020.info", "WBStartup",
                                   "FreeWheel.info"))),
        default=True,
    ),
    Package(
        "clicktofront", "ClickToFront",
        "Click anywhere in a window to bring it to the front, instead of "
        "aiming for the depth gadget. Bryce Nesbitt's 1987 original asks "
        "before it patches, so it is put where you can run it rather than "
        "started at boot.",
        category=Category.LOOK,
        download=Download("util/mouse/ClickToFront.lha",
                          (("ClickToFront/ClickToFront",
                            "Utilities/ClickToFront"),
                           ("ClickToFront/ClickToFront.info",
                            "Utilities/ClickToFront"))),
        note="In Utilities/ClickToFront. Run it once per boot; it asks "
             "before installing its patch.",
    ),
    Package(
        "dockit", "Dock-It",
        "A dock along the edge of the screen to launch what you use most. "
        "Light enough for an OCS machine, unlike the start-menu sort.",
        category=Category.EXTRAS,
        download=Download("util/wb/Dock-It375.lha",
                          (("Dock-It", "Utilities/Dock-It"),
                           ("Dock", "Utilities/Dock-It/Dock"),
                           ("dock.cfg", "Utilities/Dock-It"))),
    ),
    Package(
        "visage", "Visage",
        "A picture viewer that handles the formats datatypes do not.",
        category=Category.EXTRAS,
        download=Download("gfx/show/Visage.lha", stage="Utilities/Visage"),
        note="Unpacked into Utilities/Visage, ready to run.",
    ),
    Package(
        "sysinfo", "SysInfo",
        "What this Amiga actually is and how fast it goes - CPU, chipset, "
        "boards, and the benchmarks everyone quotes at each other.",
        category=Category.EXTRAS,
        #  4.0 gurus on a 68040 with no FPU, which is exactly what Emu68
        #  provides, and Aminet still carries a patch for it. Its own history
        #  records the fix twice over - "68040 non FPU guru fixed" in 4.3 and
        #  "68040/68060 non FPU guru fixed, again!" in 4.4 - and 4.4 is what
        #  this address serves, so the patch is not needed.
        download=Download("util/moni/SysInfo.lha", stage="Utilities/SysInfo"),
        #  Verified on ClassicWB FULL v28: Tools/SysInfo holds SysInfo 3.24
        #  (07-Nov-93) and its own docs, and nothing else.
        supersedes=("Tools/SysInfo",),
        note="Unpacked into Utilities/SysInfo, ready to run.",
    ),
    Package(
        "snoopdos", "SnoopDos",
        "Shows what a program is looking for and failing to find. The first "
        "thing to reach for when something will not start.",
        category=Category.EXTRAS,
        download=Download("util/moni/SnoopDos.lha",
                          stage="Utilities/SnoopDos"),
        note="Unpacked into Utilities/SnoopDos, ready to run.",
    ),
    Package(
        "diropus4", "Directory Opus 4",
        "A two-pane file manager, and a considerable step up from moving "
        "things about in Workbench windows. Released under the GPL, so this "
        "is the current 4.18 rather than whatever a donor happened to hold.",
        category=Category.EXTRAS,
        download=Download(
            "util/dopus/DirectoryOpus-4.18.22.lha",
            #  Its own installer copies these four into the system drawers and
            #  the program into one of its own; doing it here means the card
            #  arrives with Opus working rather than with an installer on it.
            (("DOpus4/DirectoryOpus", "Programs/DirectoryOpus"),
             ("DOpus4/DirectoryOpus.info", "Programs/DirectoryOpus"),
             ("DOpus4/Modules", "Programs/DirectoryOpus/Modules"),
             ("DOpus4/C", "C"),
             ("DOpus4/Libs", "Libs"),
             ("DOpus4/S", "S"))),
        #  Verified on ClassicWB FULL v28: Programs/DirOpus4 holds Directory
        #  Opus 4.16 (Jun 2001) with its own Docs, c, libs and modules - the
        #  same program this installs as 4.18.22, under the distribution's
        #  own name for it.
        supersedes=("Programs/DirOpus4",),
    ),

    # ------------------------------------------------------ music and pictures
    Package(
        "ahi", "AHI",
        "The Amiga's standard audio interface. Programs ask AHI for sound "
        "instead of driving Paula themselves, so they share the hardware "
        "rather than fighting over it, and a stock machine gets 14-bit "
        "output instead of 8.",
        category=Category.MEDIA,
        #  Only the prefs program needs it, but it is the only way to choose
        #  a mode afterwards. The BGUI build would avoid the dependency and
        #  ships a bgui.library carrying floating point instructions, which
        #  on a PiStorm's FPU-less 68040 is guru 8000000B.
        requires=("mui",),
        download=Download(
            "driver/audio/ahiusr_4.18.lha",
            #  What the archive's own installer copies, minus the drivers for
            #  sound cards this machine has not got. The plain ahi.device is
            #  the 68020+ build, which is what Emu68 presents; none of these
            #  binaries contains a floating point instruction.
            (("AHI/User/Devs/ahi.device", "Devs"),
             ("AHI/User/Devs/AHI/paula.audio", "Devs/AHI"),
             ("AHI/User/Devs/AudioModes/PAULA", "Devs/AudioModes"),
             #  The AUDIO: handler and its mountlist go together: ClassicWB's
             #  Startup-Sequence mounts DEVS:DOSDrivers/~(#?.info), so a
             #  driver shipped without its handler is a boot-time error.
             ("AHI/User/Devs/DOSDrivers/AUDIO", "Devs/DOSDrivers"),
             ("AHI/User/Devs/DOSDrivers/AUDIO.info", "Devs/DOSDrivers"),
             ("AHI/User/L/AHI-Handler", "L"),
             ("AHI/User/C/AddAudioModes", "C"),
             ("AHI/User/Prefs/AHI.info", "Prefs"),
             ("AHI/User/Help/ahi.guide", "Storage/Install/AHI")),
            #  The archive keeps two prefs programs side by side; the one
            #  that lands has to be called AHI for its icon to find it.
            rename=(("AHI/User/Prefs/AHI_MUI", "Prefs", "AHI"),)),
        note="Installed, not staged: ahi.device and the Paula driver go "
             "straight into DEVS:, and AHI Prefs into Prefs. Only the Paula "
             "driver is copied - the Toccata and Delfina drivers are for "
             "sound cards this machine has not got.",
    ),
    Package(
        "amplifier", "AMPlifier",
        "A multiformat audio player: modules, MP3 and the rest, with skins.",
        category=Category.MEDIA,
        download=Download("mus/play/AMPlifier.lha", stage="Audio/AMPlifier"),
        note="Unpacked into Audio/AMPlifier, ready to run.",
    ),
    Package(
        "hippoplayer", "HippoPlayer",
        "The classic lightweight module player, small enough to leave "
        "running while something else works.",
        category=Category.MEDIA,
        download=Download("mus/play/hippoplayer.lha",
                          (("HippoPlayer", "Audio/HippoPlayer"),
                           ("HippoSupport", "Audio/HippoSupport"))),
    ),
    Package(
        "digibooster", "DigiBooster 1.7",
        "An eight channel tracker, the full version rather than a demo.",
        category=Category.MEDIA,
        download=Download("mus/edit/DigiBooster1_7.lha",
                          stage="Audio/DigiBooster"),
        note="Unpacked into Audio/DigiBooster, ready to run.",
    ),
    Package(
        "scalos", "Scalos",
        "A complete Workbench replacement. Handsome, and hungry: worth it on "
        "AGA or an RTG screen, a poor trade on a plain OCS desktop.",
        category=Category.LOOK,
        download=Download("util/wb/Scalos.lha", stage=STAGING + "/Scalos"),
        chipsets=(Chipset.AGA,),
        or_rtg=True,
        #  One of the few things that genuinely cannot be installed from here:
        #  its script picks between three builds of every module for the
        #  machine it finds, and replacing the desktop half-way is how a card
        #  stops booting.
        note="Unpacked into Storage/Install/Scalos. Run its Install.Scalos on "
             "the Amiga - it chooses between three builds of each module for "
             "the machine it finds, which cannot be decided from here.",
    ),

    # ------------------------------------------------------------- speed
    Package(
        "fblit", "FBlit",
        "Moves Workbench's drawing off the Amiga's blitter and into fast RAM, "
        "which is where a PiStorm's speed actually lives. The single biggest "
        "win for a native Workbench with more than a few colours.",
        category=Category.SPEED,
        native_only=True,
        download=Download("util/boot/fblit.lha",
                          (("FBlit/FBlit", "C"),
                           ("FBlit/fblit.library", "Libs"))),
        startup=("C:FBlit >NIL:",),
    ),
    Package(
        "ftext", "FText",
        "The companion to FBlit that does the same for text rendering.",
        category=Category.SPEED,
        native_only=True,
        download=Download("util/boot/FText.lha", (("FText", "C"),)),
        startup=("C:FText >NIL:",),
    ),
    Package(
        "picasso96", "Picasso96",
        "The RTG subsystem. Only useful where there is an RTG display to draw "
        "on - the Pi's HDMI output.",
        category=Category.SPEED,
        download=Download(
            "driver/video/Picasso96.lha",
            #  Installed from its own archive, not assembled out of whatever
            #  the source drive happened to carry. RTG used to depend on the
            #  imported system having a Picasso96 monitor to adapt: build on
            #  a distribution without one - ClassicWB has none - and the card
            #  came out with VideoCore.card in LIBS: and nothing able to load
            #  it, which is a feature that works or does not depending on
            #  where the drive came from.
            (("Picasso96Install", STAGING + "/Picasso96"),
             ("Picasso96Install/Libs/Picasso96API.library", "Libs"),
             ("Picasso96Install/Libs/Picasso96/fastlayers.library",
              "Libs/Picasso96"),
             ("Picasso96Install/Devs/Monitors/Picasso96", "Devs/Monitors"),
             ("Picasso96Install/Devs/Monitors/Picasso96.info",
              "Devs/Monitors"),
             ("Picasso96Install/Prefs/Picasso96Mode", "Prefs"),
             ("Picasso96Install/Prefs/Picasso96Mode.info", "Prefs")),
            #  The archive ships one settings file per monitor frequency and
            #  its installer asks which. Emu68's output is HDMI, so the most
            #  permissive of them is the one that does not needlessly cut the
            #  mode list short.
            rename=(("Picasso96Install/Devs/Picasso96Settings.64", "Devs",
                     "Picasso96Settings"),)),
        rtg_only=True,
        #  Choosing an RTG display *is* choosing Picasso96: it is the RTG
        #  subsystem, and Emu68's driver is a card for it. Leaving it to be
        #  ticked separately meant asking for the Pi's HDMI output and being
        #  handed a card with no screen modes to show on it.
        essential=True,
        note="Installed, not staged: Picasso96API.library, its own monitor "
             "and settings, with Emu68's VideoCore driver as the board. The "
             "full archive is still in Storage/Install if you want the "
             "datatypes and painting-program drivers as well.",
    ),

    # -------------------------------------------------------- networking
    Package(
        "wifipi", "The Pi's WiFi as an Amiga network card",
        "Emu68's own driver for the wireless chip on the Pi, so the Amiga "
        "has something for a TCP/IP stack to talk to. The network it joins "
        "is the one set on the Amiga page; the firmware for every Pi model "
        "is installed with it.",
        category=Category.NETWORK,
        #  Where the network device used to come from was a donor's
        #  vlink.device, which is the PiStorm firmware's emulated Ethernet
        #  and is not published anywhere this can fetch. Emu68's own release
        #  carries a driver for the hardware the Pi actually has.
        download=Download(
            "https://github.com/michalsc/Emu68-tools/releases/download/"
            "v1.1/Emu68-tools.zip",
            (("Emu68-WiFi/Devs/Networks/wifipi.device", "Devs/Networks"),
             ("Emu68-WiFi/Devs/Firmware", "Devs/Firmware")),
            source="the Emu68-tools release"),
        note="Needs the WiFi network filled in on the Amiga page: the driver "
             "reads the same wpa_supplicant.conf the Pi is given.",
    ),

    Package(
        "roadshow", "Roadshow (TCP/IP stack)",
        "The TCP/IP stack most PiStorm machines run. It installs its own "
        "bsdsocket.library, which is what the browsers, the FTP clients and "
        "the IRC clients open, so nothing here can reach a network without a "
        "stack of some kind.",
        category=Category.NETWORK,
        #  APC&TCP publish the demo through a page that serves it only to a
        #  browser, so there is no address this can fetch.  The archive has
        #  to be put in the cache by hand, and the build says so when it is
        #  not there rather than quietly leaving the card without a stack.
        download=Download("Roadshow-Demo-1.15.lha", merge=True,
                          stage=STAGING + "/Roadshow", manual=True,
                          source="http://roadshow.apc-tcp.de/ "
                                 "(Download, then Demoversion)",
                          #  Roadshow's own S/User-Startup is four lines meant
                          #  to be added to the card's, not to replace it.
                          skip=("S/User-Startup",),
                          #  Every interface template in the archive is for
                          #  somebody else's hardware. The card gets one for
                          #  the device this tool actually installs.
                          write=(("wifipi", "Devs/NetInterfaces",
                                  "# Written by the PiStorm imager.\n"
                                  "# The Pi's own WiFi, as installed by the"
                                  " network card package.\n"
                                  "device=wifipi.device\n"
                                  "unit=0\n"
                                  "configure=dhcp\n"
                                  "requiresinitdelay=no\n"),)),
        #  The lines Roadshow's installer would have added to User-Startup.
        startup=("IF EXISTS S:Network-Startup",
                 "   Execute S:Network-Startup",
                 "EndIF"),
        note="The free demo is the full stack with each network session "
             "limited to 15 minutes; the unlimited version is sold by "
             "APC&TCP. Roadshow does put bsdsocket.library in LIBS:, so on a "
             "card that also runs WHDLoad games, add C:NetShutdown to "
             "S:WHDLoad-Startup to take the stack down while a game runs.",
    ),
    Package(
        "amissl", "AmiSSL",
        "TLS for the Amiga. Without it almost nothing on the modern web will "
        "answer.",
        category=Category.NETWORK,
        download=Download("util/libs/AmiSSL-v5-OS3.lha",
                          stage=STAGING + "/AmiSSL"),
        note="Run its Installer from Storage/Install on the Amiga.",
    ),
    Package(
        "netsurf", "NetSurf",
        "A browser that renders modern HTML and CSS, and the most usable one "
        "on 68k hardware.",
        category=Category.NETWORK,
        download=Download("comm/www/netsurf-m68k.lha",
                          stage="Internet/NetSurf"),
        requires=("mui",),
        note="Unpacked into Internet/NetSurf, ready to run.",
    ),
    Package(
        "amftp", "AmFTP",
        "An FTP client, which is still how most Amiga file transfer is done.",
        category=Category.NETWORK,
        download=Download("comm/tcp/AmFTP191.lha", stage="Internet/AmFTP"),
        requires=("mui",),
        note="Unpacked into Internet/AmFTP, ready to run.",
    ),
    Package(
        "wookiechat", "WookieChat",
        "An IRC client.",
        category=Category.NETWORK,
        download=Download(
            "comm/irc/WookieChat2.11_OS3.lha",
            #  Its installer copies these into the system drawers; the
            #  program cannot open a window without the MUI classes, and
            #  cannot decode anything it is sent without codesets.
            (("WookieChat2.11_OS3_Installer", "Internet/WookieChat"),
             ("WookieChat2.11_OS3_Installer/libs", "Libs"),
             ("WookieChat2.11_OS3_Installer/MUI/OS3", "Libs/MUI"))),
        requires=("mui",),
    ),

]

CATALOGUE_BY_KEY = {p.key: p for p in CATALOGUE}


def expand(keys: Iterable[str]) -> list[str]:
    """``keys`` plus everything they require, dependencies first.

    Order matters: a dependency's files should be on the card, and its lines
    in ``S:User-Startup``, before whatever needs it.
    """
    out: list[str] = []

    def add(key: str, seen: tuple[str, ...] = ()) -> None:
        if key in out or key in seen:
            return                      # already added, or a cycle
        package = CATALOGUE_BY_KEY.get(key)
        if package is None:
            return
        for need in package.requires:
            add(need, seen + (key,))
        if key not in out:
            out.append(key)

    for key in keys:
        add(key)
    return out


def in_category(category: Category) -> list[Package]:
    return [p for p in CATALOGUE if p.category is category]


# ------------------------------------------------------------ downloads

def cache_dir() -> Path:
    from .emu68 import cache_dir as emu68_cache
    folder = emu68_cache() / "packages"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _extractor() -> list[str] | None:
    for command in (["7z", "x", "-y"], ["7za", "x", "-y"], ["lha", "-xw"]):
        if shutil.which(command[0]):
            return command
    return None


def download_archive(package: Package, progress: Progress) -> Path | None:
    """Fetch a package's archive, reusing the cached copy when there is one."""
    if package.download is None:
        return None
    target = cache_dir() / package.download.filename
    #  The cache is keyed on the file name, and two publishers can use the
    #  same one: moving WHDLoad from Aminet to its author's site changed
    #  nothing at all, because both serve "WHDLoad_usr.lha" - so cards went
    #  on being built from a 2007 archive that was already in the cache.
    #  Remember where a copy came from, and fetch again when that changes.
    note = target.with_name(target.name + ".source")
    if target.exists() and target.stat().st_size:
        came_from = note.read_text().strip() if note.exists() else ""
        if came_from == package.download.url or package.download.manual:
            progress.log(f"  {package.label}: using cached "
                         f"{target.name} ({human_size(target.stat().st_size)})")
            return target
        progress.log(f"  {package.label}: the cached {target.name} came from "
                     f"{came_from or 'somewhere unrecorded'}, so it is being "
                     f"fetched again")
    if package.download.manual:
        progress.log(f"  {package.label}: {target.name} is not in the cache, "
                     f"and it cannot be downloaded automatically. Fetch it "
                     f"from {package.download.source} and put it in "
                     f"{cache_dir()}, then build again. Skipped.")
        return None
    progress.log(f"  {package.label}: downloading {package.download.url}")
    request = urllib.request.Request(package.download.url,
                                     headers={"User-Agent": USER_AGENT})
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, \
                open(temporary, "wb") as out:
            shutil.copyfileobj(response, out)
            declared = response.headers.get("Content-Length")
        #  A download that stops early is still a file, and caching it means
        #  every build afterwards fails to unpack an archive that looks like
        #  it is already there. Check the length while the answer is at hand.
        written = temporary.stat().st_size
        if declared is not None and written != int(declared):
            temporary.unlink(missing_ok=True)
            progress.log(f"  {package.label}: download stopped early "
                         f"({human_size(written)} of {human_size(int(declared))}"
                         f"), not kept")
            return None
    except Exception as error:                    # noqa: BLE001 - reported
        temporary.unlink(missing_ok=True)
        progress.log(f"  {package.label}: download failed ({error}), skipped")
        return None
    temporary.replace(target)
    note.write_text(package.download.url + "\n")
    progress.log(f"  {package.label}: {human_size(target.stat().st_size)}")
    return target


#  The first word of an LhA header is the header size and its checksum; the
#  method identifier sits two bytes in.  These are the ones Amiga archives use.
LHA_METHODS = (b"-lh0-", b"-lh1-", b"-lh4-", b"-lh5-", b"-lh6-", b"-lh7-")


def embedded_archive(path: Path) -> Path | None:
    """The LhA archive inside a self-extracting Amiga program.

    An archiver has to be distributed as one of these - you need an archiver
    to unpack an archive - so Aminet ships LhA as ``lha.run``: a small Amiga
    executable with the real archive appended. Nothing here can run an Amiga
    program, and leaving it on the card meant handing over a card with no
    archiver until somebody found and ran the extractor.

    The stub carries a tiny archive of its own (its usage text), so the
    *second* header is the payload. Returns None when the file holds nothing
    that looks like one, rather than guessing.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    starts = [i for i in range(len(data) - 7)
              if data[i + 2:i + 7] in LHA_METHODS]
    if len(starts) < 2:
        return None
    out = cache_dir() / (path.stem + "-payload.lha")
    out.write_bytes(data[starts[1]:])
    return out


def unpack(archive: Path, progress: Progress) -> Path | None:
    """Unpack an LhA archive into the cache, once, and return the directory."""
    destination = cache_dir() / (archive.stem + ".unpacked")
    if destination.is_dir() and any(destination.iterdir()):
        #  The same trap as the archive cache, one level down: fetching a
        #  newer archive is no use if what was unpacked from the old one is
        #  handed back. Every package was correctly re-downloaded and then
        #  installed from the tree unpacked hours earlier, so a card came out
        #  carrying WHDLoad 16.8 while the archive beside it was 20.0.
        if destination.stat().st_mtime >= archive.stat().st_mtime:
            return destination
        progress.log(f"  {archive.name} has changed since it was last "
                     f"unpacked; unpacking it again")
        shutil.rmtree(destination, ignore_errors=True)
    if archive.suffix.lower() == ".run":
        payload = embedded_archive(archive)
        if payload is None:
            progress.log(f"  {archive.name} is a self-extracting program and "
                         f"no archive could be found inside it")
            return None
        progress.log(f"  {archive.name}: took the archive out of the "
                     f"self-extractor")
        archive = payload
    command = _extractor()
    if command is None:
        progress.log("  no 7z or lha available to unpack Amiga archives")
        return None
    destination.mkdir(parents=True, exist_ok=True)
    #  7-Zip wants the output directory glued to the switch, with no space.
    arguments = list(command)
    if arguments[0] in ("7z", "7za"):
        arguments.append(f"-o{destination}")
    result = subprocess.run(arguments + [str(archive)], cwd=destination,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode != 0 or not any(destination.iterdir()):
        progress.log(f"  could not unpack {archive.name}: "
                     f"{result.stderr.decode('utf-8', 'replace').strip()[:120]}")
        return None
    return destination


def _written(package: Package, progress: Progress) -> list[tuple[str, str]]:
    """The files this tool writes itself for a package.

    Any download can have them, not only one laid out drawer by drawer: a
    settings file that says which of a program's optional pieces this machine
    can actually use is exactly that sort of thing.
    """
    out: list[tuple[str, str]] = []
    for name, destination, text in package.download.write:
        made = cache_dir() / f"{package.key}-written" / destination
        made.mkdir(parents=True, exist_ok=True)
        (made / name).write_text(text)
        out.append((str(made / name), destination))
        progress.log(f"  {package.label}: wrote {destination}/{name}")
    return out


def _merged(package: Package, root: Path,
            progress: Progress) -> list[tuple[str, str]]:
    """Place an archive laid out like a Workbench disk, drawer by drawer.

    Nothing is dropped silently: whatever is not a system drawer - the docs,
    the publisher's own installer - is staged where the user can find it, and
    said so in the log.
    """
    inner = [p for p in root.iterdir() if p.is_dir()]
    loose = [p for p in root.iterdir()
             if p.is_file() and not p.name.lower().endswith(".info")]
    if len(inner) == 1 and not loose:
        root = inner[0]
    #  A distribution built around an installer keeps the part that is shaped
    #  like a Workbench disk in a drawer of its own; the rest is documentation
    #  and the installer script, which belong on the card only as staging.
    shaped = next((c for c in root.iterdir()
                   if c.is_dir() and c.name.lower() in ("workbench", "amiga")),
                  None)
    known = {name.lower(): name for name in SYSTEM_DRAWERS}
    pairs: list[tuple[str, str]] = []
    staged: list[str] = []
    skip = {s.lower() for s in package.download.skip}
    for entry in sorted((shaped or root).iterdir(), key=lambda e: e.name.lower()):
        target = known.get(entry.name.lower())
        if target and entry.is_dir():
            pairs += _drawer(entry, target, skip)
        elif entry.name.lower().endswith(".info"):
            continue
        elif shaped is None:
            staged.append(entry.name)
            pairs.append((str(entry), package.download.stage or STAGING))
    if shaped is not None:
        for entry in sorted(root.iterdir(), key=lambda e: e.name.lower()):
            if entry == shaped or entry.name.lower().endswith(".info"):
                continue
            staged.append(entry.name)
            pairs.append((str(entry), package.download.stage or STAGING))
    if staged:
        progress.log(f"  {package.label}: staged {', '.join(staged)}")
    return pairs


def _drawer(drawer: Path, target: str,
            skip: set[str]) -> list[tuple[str, str]]:
    """One drawer of an archive, minus anything it must not place.

    A file the card already keeps its own version of - S:User-Startup above
    all - is never placed whole: the package adds its lines through the
    startup mechanism instead.
    """
    inside = [f"{target}/{e.name}".lower() for e in drawer.iterdir()]
    if not any(name in skip for name in inside):
        return [(str(drawer), target)]
    return [(str(entry), target) for entry in sorted(drawer.iterdir())
            if f"{target}/{entry.name}".lower() not in skip]


def fetch(package: Package, progress: Progress) -> list[tuple[str, str]]:
    """Download and unpack one package, as (host path, destination) pairs."""
    archive = download_archive(package, progress)
    if archive is None:
        return []
    if package.download.raw:
        return [(str(archive), package.download.stage)]
    root = unpack(archive, progress)
    if root is None:
        return []
    download = package.download
    if download.merge:
        return _merged(package, root, progress) + _written(package, progress)
    #  Placed whole - the archive is the program, and goes where `stage` says.
    #  This used to be chosen on `items` alone, so a package that placed its
    #  files by `rename` or wrote its own returned here instead, and its whole
    #  archive went to `stage` - which for such a package is "", the volume
    #  root.
    if not (download.items or download.rename or download.write):
        inner = [p for p in root.iterdir() if p.is_dir()]
        source = inner[0] if len(inner) == 1 else root
        return [(str(source), download.stage)]
    out: list[tuple[str, str]] = _written(package, progress)
    for inside, destination, newname in download.rename:
        source = root / inside
        if not source.exists():
            progress.log(f"  {package.label}: {inside} is not in the archive")
            continue
        staged = cache_dir() / f"{package.key}-renamed" / destination
        staged.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, staged / newname)
        out.append((str(staged / newname), destination))
        progress.log(f"  {package.label}: {Path(inside).name} installed as "
                     f"{destination}/{newname}")
    for inside, destination in download.items:
        path = root / inside
        if path.exists():
            out.append((str(path), destination))
        else:
            progress.log(f"  {package.label}: {inside} is not in the archive")
    return out


# -------------------------------------------------------------- choosing

def suits(key: str, chipset: Chipset, display: Display) -> bool:
    package = CATALOGUE_BY_KEY.get(key)
    return package is not None and package.suits(chipset, display)


def overlays_for(keys: list[str],
                 rtg: bool = True,
                 chipset: Chipset = Chipset.AGA,
                 display: Display | None = None,
                 progress: Progress | None = None,
                 allow_download: bool = True) -> list[tuple[str, str]]:
    """The pairs alone, for callers that do not care which package gave them."""
    return [pair for _key, pairs in overlays_by_package(
        keys, rtg, chipset, display, progress, allow_download)
        for pair in pairs]


def overlays_by_package(keys: list[str],
                        rtg: bool = True,
                        chipset: Chipset = Chipset.AGA,
                        display: Display | None = None,
                        progress: Progress | None = None,
                        allow_download: bool = True
                        ) -> list[tuple[str, list[tuple[str, str]]]]:
    """Turn chosen packages into (source, destination) pairs to copy.

    Everything comes from its publisher - Aminet, or the project that makes
    it. Software used to be able to come out of a donor system instead, which
    meant a card was built from whatever some other installation happened to
    hold, at whatever age; every package here now names where it comes from.
    """
    if display is None:
        display = Display.RTG_HDMI if rtg else Display.NATIVE
    progress = progress or Progress()
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(pairs: list[tuple[str, str]]) -> None:
        for pair in pairs:
            #  Several packages lean on the same library - three of them want
            #  codesets - and copying one file twice is not merely wasteful:
            #  the writer creates files and refuses to overwrite, so the
            #  second copy would end the build.
            if pair not in seen:
                seen.add(pair)
                out.append(pair)

    by_package: list[tuple[str, list[tuple[str, str]]]] = []
    for key in expand(keys):
        package = CATALOGUE_BY_KEY.get(key)
        if package is None or not package.suits(chipset, display):
            continue
        if not allow_download or package.download is None:
            continue
        fetched = fetch(package, progress)
        if not fetched and progress is not None:
            progress.log(f"  WARNING: {package.label} could not be fetched "
                         f"from {package.download.where}, so it is not on "
                         f"this card")
        before = len(out)
        add(fetched)
        #  Only what this package actually contributed: a library two of them
        #  want belongs to whichever asked first, and listing it twice would
        #  say the card has two of it.
        if len(out) > before:
            by_package.append((key, out[before:]))
    return by_package


def default_keys(rtg: bool = True) -> list[str]:
    return [p.key for p in CATALOGUE if p.default and (rtg or not p.rtg_only)]


def suggested(machine: Machine, display: Display, *,
              networking: bool = False) -> list[str]:
    """A sensible set for this machine and this screen.

    The reasoning, in one place rather than scattered through the interface:

    * The CPU patches are deliberately NOT here.  Newer SetPatch and CPU
      libraries look like an obvious win on a 68040-class machine, and they
      stop every WHDLoad game from running - which is what these cards are
      mostly for.
    * Everything needs WHDLoad, an archiver and Installer.
    * A faster icon.library is free speed on any machine.
    * On a native screen the cost is the chipset drawing it, so FBlit, FText
      and a locked palette earn their place.
    * On an RTG screen there is no blitter in the way; Picasso96 is the point
      of it, and a heavier desktop becomes affordable.
    """
    chosen = ["whdload", "lha", "installer", "igame", "iconlib",
              "magicmenu", "visualprefs"]
    if display.uses_native and machine.chipset is not Chipset.NONE:
        chosen += ["fblit", "ftext", "fullpalette"]
    if display.uses_rtg:
        chosen += ["picasso96"]
    if machine.aga or display.uses_rtg:
        chosen += ["scalos"]
    if networking:
        chosen += ["wifipi", "roadshow", "amissl", "netsurf"]
    return [key for key in chosen if suits(key, machine.chipset, display)]
