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

    @property
    def url(self) -> str:
        if self.path.startswith(("http://", "https://")):
            return self.path
        return AMINET + self.path

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclasses.dataclass(frozen=True)
class Package:
    key: str
    label: str
    description: str
    category: Category = Category.SYSTEM
    #  (path within the donor system, destination within the target drive).
    #  A source that is a directory is copied whole; a file is copied into the
    #  destination drawer.
    items: tuple[tuple[str, str], ...] = ()
    download: Download | None = None
    default: bool = False
    #  Only worth having where the Pi's HDMI is driving an RTG screen.
    rtg_only: bool = False
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
    #  Files the package needs but which are not the package: the shared
    #  libraries and classes it draws with.  Kept apart from ``items``
    #  because ``items`` means "the donor's copy of this package", and a
    #  package taken from Aminet instead still needs these.  They are always
    #  taken from a donor, never downloaded, and a missing one is not fatal.
    support: tuple[tuple[str, str], ...] = ()
    note: str = ""

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
        items=(("C/WHDLoad", "C"), ("Expansion/WHDLoad", "Expansion/WHDLoad"),
               #  Its settings live here, not in the command: the quit key,
               #  whether it forces PAL, and the hooks it runs around a game.
               ("S/WHDLoad.prefs", "S")),
        #  Nearly every slave asks WHDLoad for the Kickstart the game expects
        #  and will not start without it.  These are ROM images rather than
        #  code, so nothing names them inside a binary and no scan can find
        #  them - they have to be asked for.  Without them iGame launches a
        #  game and the machine falls over on the spot.
        support=(("Devs/Kickstarts", "Devs/Kickstarts"),),
        #  These were once required here, on the reasoning that a 68040
        #  needs modern CPU support.  Tested, the opposite is true: either
        #  of them stops every WHDLoad game dead.  See their own entries.

        download=Download("dev/misc/WHDLoad_usr.lha",
                          (("WHDLoad/C/WHDLoad", "C"),
                           ("WHDLoad/C/WHDLoadCD32", "C"),
                           ("WHDLoad/C/Patcher", "C"))),
        default=True,
    ),
    Package(
        "lha", "LhA",
        "The archiver Amiga software is distributed in. Without it very little "
        "downloaded from Aminet can be unpacked.",
        items=(("C/lha", "C"),),
        download=Download("util/arc/lha.run", stage=STAGING + "/LhA",
                          raw=True),
        default=True,
        note="Aminet ships LhA as a self-extracting Amiga program; run "
             "lha.run from Storage/Install on the Amiga.",
    ),
    Package(
        "installer", "Installer",
        "Commodore's installer, which most third-party install scripts expect "
        "to find and fail without.",
        items=(("C/Installer", "C"),),
        download=Download("util/misc/Installer-43_3.lha",
                          (("Installer43_3/Installer", "C"),)),
        default=True,
    ),
    Package(
        "igame", "iGame",
        "A launcher that lists WHDLoad games with their screenshots.",
        items=(("Programs/iGame", "Programs/iGame"),),
        #  iGame draws its screenshots through these two.
        support=(("Libs/guigfx.library", "Libs"),
                 ("Libs/render.library", "Libs")),
        requires=("mui",),
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
        "setpatch", "A SetPatch that knows about the 68040",
        "Workbench 3.1 ships SetPatch 40.16, from 1994 - it predates the "
        "68040 and does not set one up. Newer, but it stops WHDLoad games "
        "starting, so it is off unless you know you want it.",
        category=Category.UPDATES,
        #  Only a donor can supply this: it is Commodore's, from a later
        #  release, and is not on Aminet.  Without it WHDLoad takes a
        #  privilege violation the moment it tries to start a game, because
        #  the CPU it is running on was never properly set up.
        items=(("C/SetPatch", "C"),),
        support=(("C/PatchRAM", "C"),),
        #  Same story as MMULib: replacing Commodore's SetPatch leaves
        #  WHDLoad games hanging on a black screen.  Tested one variable at
        #  a time against a card that runs the game.
        note="Do not install this on a card for games: WHDLoad titles hang "
             "instead of starting.",
    ),
    Package(
        "mui", "MUI",
        "Magic User Interface: the toolkit a great deal of Amiga software "
        "draws itself with. Nothing that needs it will start without it.",
        #  MUI is not a drawer of files that can be scattered into LIBS: - it
        #  expects to be found through a MUI: assign, with its own libraries
        #  and locale added to the system's.  This is how a real MUI install
        #  is arranged, and how the donor systems carry it.
        items=(("System/MUI", "System/MUI"),),
        #  MUI reads its configuration from ENV:MUI, which Workbench fills
        #  from ENVARC: at boot.  Without it every MUI application starts on
        #  built-in defaults and loses whatever the donor had set up.
        #  MUI's own key is picked up by the key rule in
        #  resolve_dependencies, which matches it to this drawer's name.
        support=(("Prefs/Env-Archive/mui", "Prefs/Env-Archive/mui"),),
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
        "identify", "identify.library",
        "Lets tools name the hardware they are running on. A dependency of "
        "several of the others.",
        items=(("Libs/identify.library", "Libs"),),
        download=Download("util/libs/Identify.lha",
                          (("Identify/libs/identify.library", "Libs"),)),
    ),
    Package(
        "copyicon", "CopyIcon",
        "Copies an icon's image onto another file, which is how a hand-made "
        "icon set gets applied.",
        items=(("C/CopyIcon", "C"),),
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
        items=(("L/ToolsDaemon-Handler", "L"),),
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
        items=(("Libs/icon.library", "Libs"),
               ("Libs/workbench.library", "Libs"),
               ("C/LoadModule", "C")),
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
        "magicwb", "MagicWB",
        "The classic eight-colour icon and font set. Designed for exactly the "
        "kind of limited palette a native Workbench has.",
        category=Category.LOOK,
        #  Its fonts and patterns are ordinary files and are installed here;
        #  its icon set is used to give this build's own drawers icons (see
        #  ``icon_set_dirs``).  Only the parts that replace icons already on
        #  the card still need its Installer, because the file system this
        #  tool writes creates files and never overwrites them.
        download=Download("util/wb/MagicWB21p.lha",
                          (("MagicWB2.1p/Fonts", "Fonts"),
                           ("MagicWB2.1p/Patterns", "Prefs/Presets"),
                           ("MagicWB2.1p", STAGING + "/MagicWB"))),
        note="Fonts and patterns are installed; run its Installer from "
             "Storage/Install to restyle the icons already on the card.",
    ),
    Package(
        "magicmenu", "MagicMenu",
        "Turns the menu bar into a pop-up menu under the pointer, instead of a "
        "trip to the top of the screen.",
        category=Category.LOOK,
        items=(("WBStartup/MagicMenu", "WBStartup"),),
        download=Download("util/wb/MagicMenu_3.1.lha",
                          (("MagicMenu/WBStartup/MagicMenu", "WBStartup"),)),
    ),
    Package(
        "visualprefs", "VisualPrefs",
        "Redraws window borders and gadgets - thin, flat, modern instead of "
        "the stock bevels. Patches the GUI, so it installs itself on the Amiga.",
        category=Category.LOOK,
        items=(("Prefs/VisualPrefs", "Prefs"),),
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
                           ("FullPalette/FPPrefs", "Prefs"))),
    ),
    Package(
        "newicons", "NewIcons",
        "Icons that redraw themselves in whatever palette the screen has. "
        "Patches the system, so it installs itself on the Amiga.",
        category=Category.LOOK,
        items=(("C/NewIcons", "C"), ("Prefs/NewIcons", "Prefs")),
        support=(("Libs/newicon.library", "Libs"),),
        download=Download("util/wb/NewIcons46.lha", stage=STAGING + "/NewIcons"),
        note="Run its Installer from Storage/Install on the Amiga.",
    ),
    Package(
        "birdie", "Birdie",
        "Patterns the window borders, which softens the stock look for very "
        "little memory.",
        category=Category.LOOK,
        items=(("C/Birdie", "C"),),
        download=Download("util/wb/birdie2000.lha", stage=STAGING + "/Birdie"),
        note="Copy it into C: and start it from your user-startup.",
    ),
    Package(
        "powerwindows", "PowerWindows",
        "Makes windows move and resize smoothly rather than as an outline.",
        category=Category.LOOK,
        items=(("Programs/PowerWindows", "Programs/PowerWindows"),),
        download=Download("util/misc/PowerWindows.lha",
                          stage=STAGING + "/PowerWindows"),
        note="Copy it into WBStartup on the Amiga.",
    ),
    Package(
        "deficons", "DefIcons",
        "Gives every file an icon chosen from what it actually is. Without it "
        "a Workbench window shows programs and nothing else, which is most of "
        "why a stock 3.1 desktop looks so bare.",
        category=Category.LOOK,
        download=Download("util/wb/DefIcons44.lha",
                          (("DefIcons44/DefIcons44", "WBStartup"),
                           ("DefIcons44/DefIconsPrefs", "Prefs"),
                           ("DefIcons44/deficons.prefs",
                            "Prefs/Env-Archive"))),
        default=True,
    ),
    Package(
        "freewheel", "FreeWheel",
        "Mouse wheel support: scrolls the window under the pointer. Its "
        "absence is one of the first things anyone notices.",
        category=Category.LOOK,
        #  The 020 build, since a PiStorm is a 68040.
        download=Download("util/mouse/FreeWheel.lha",
                          (("FreeWheel/FreeWheel_020", "WBStartup"),
                           ("FreeWheel/FreeWheel.cfg", "S"))),
        default=True,
    ),
    Package(
        "clicktofront", "ClickToFront",
        "Click anywhere in a window to bring it to the front, instead of "
        "aiming for the depth gadget.",
        category=Category.LOOK,
        items=(("WBStartup/ClickToFront", "WBStartup"),),
    ),
    Package(
        "backdrops", "Backdrops and boot pictures",
        "The wallpapers and boot pictures from the system you are copying "
        "from. Several megabytes of them, so worth a thought on a small "
        "system partition.",
        category=Category.LOOK,
        items=(("Prefs/Presets/Backdrops", "Prefs/Presets/Backdrops"),
               ("Prefs/Presets/BootPics", "Prefs/Presets/BootPics")),
    ),

    # ------------------------------------------------------- handy extras
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
        "things about in Workbench windows.",
        category=Category.EXTRAS,
        items=(("Programs/DirectoryOpus4", "Programs/DirectoryOpus4"),),
    ),

    # ------------------------------------------------------ music and pictures
    Package(
        "amplifier", "AMPlifier",
        "A multiformat audio player: modules, MP3 and the rest, with skins.",
        category=Category.MEDIA,
        download=Download("mus/play/AMPlifier.lha", stage="Audio/AMPlifier"),
        note="Unpacked into Audio/AMPlifier, ready to run.",
    ),
    Package(
        "hippoplayer", "HippoPlayer",
        "The classic lightweight module player. Not freely distributable, so "
        "only ever copied from a system you already have.",
        category=Category.MEDIA,
        items=(("Audio/HippoPlayer", "Audio/HippoPlayer"),),
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
        items=(("System/Scalos", "System/Scalos"), ("C/FixScalos", "C")),
        chipsets=(Chipset.AGA,),
        or_rtg=True,
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
        items=(("Libs/Picasso96", "Libs/Picasso96"),
               ("Prefs/Picasso96Mode", "Prefs"),
               ("Libs/rtg.library", "Libs")),
        download=Download("driver/video/Picasso96.lha",
                          stage=STAGING + "/Picasso96"),
        rtg_only=True,
        note="Emu68's VideoCore driver is installed for it. Run the Installer "
             "from Storage/Install on the Amiga to create the monitor and "
             "screen modes - doing that here produced a card that would not "
             "boot.",
    ),

    # -------------------------------------------------------- networking
    Package(
        "network", "TCP/IP networking",
        "The PiStorm's own network device and the MiamiDx stack, from a "
        "donor. Miami arrives unregistered and unconfigured, so it has to be "
        "set up on the Amiga before anything can use it; for a stack that "
        "works as installed, choose Roadshow instead.",
        category=Category.NETWORK,
        items=(("Devs/Networks/vlink.device", "Devs/Networks"),
               #  MiamiDx is the stack this system actually runs.  It brings
               #  its own libraries and devices, and it publishes
               #  bsdsocket.library itself, in memory, once it goes online -
               #  which is why no copy of that file is installed here.  The
               #  one in the donor's LIBS: is an orphaned AmiTCP 4.1 stub
               #  with no stack behind it, and it stops every WHDLoad game.
               ("Internet/MiamiDx", "Internet/MiamiDx"),
               ("Libs/miamibpf.library", "Libs"),
               ("Libs/miamiipnat.library", "Libs"),
               ("Libs/miamipcap.library", "Libs"),
               ("Libs/miamisecureshell.library", "Libs"),
               ("Libs/miamisocks.library", "Libs"),
               ("Libs/miamisocksd.library", "Libs"),
               ("S/miami.key1", "S"),
               ("S/miami.key2", "S"),
               ("AmiTCP", "AmiTCP"),
               ("Internet/Genesis", "Internet/Genesis")),
        #  Miami looks for its own libraries under Miami:, and its window is
        #  a MUI class, so neither the assign nor MUI is optional: without
        #  them it exits silently and nothing publishes bsdsocket.
        requires=("mui",),
        startup=("IF EXISTS SYS:Internet/MiamiDx",
                 "   Assign Miami: SYS:Internet/MiamiDx",
                 "EndIF"),
        note="Installs MiamiDx, which provides the socket library itself "
             "while it is online. Nothing is left in LIBS: when it is not, "
             "so WHDLoad games are unaffected.",
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
                          #  somebody else's hardware. A PiStorm is always
                          #  vlink.device, so the card gets one that works.
                          write=(("vlink", "Devs/NetInterfaces",
                                  "# Written by the PiStorm imager.\n"
                                  "# The PiStorm's own Ethernet, as installed"
                                  " by the networking package.\n"
                                  "device=vlink.device\n"
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
        support=(("Libs/codesets.library", "Libs"),
                 ("Libs/openurl.library", "Libs")),
        requires=("mui",),
        note="Unpacked into Internet/NetSurf, ready to run.",
    ),
    Package(
        "aweb", "AWeb",
        "The lighter classic browser, now freely distributable. Quicker than "
        "NetSurf on a plain native screen.",
        category=Category.NETWORK,
        #  AWeb draws with ReAction, whose gadget classes live in CLASSES: -
        #  without them it opens no window at all.
        items=(("Internet/AWeb_APL", "Internet/AWeb"),),
        #  ReAction is the gadget classes *and* the window and requester
        #  classes that hold them; Gadgets alone still opens nothing.  AWeb
        #  itself is reached through an assign - the donor system makes one -
        #  and both it and ReAction keep settings in ENVARC.
        support=(("Classes/Gadgets", "Classes/Gadgets"),
                 ("Classes/window.class", "Classes"),
                 ("Classes/requester.class", "Classes"),
                 ("Classes/arexx.class", "Classes"),
                 ("Classes/startup.class", "Classes"),
                 ("Libs/codesets.library", "Libs"),
                 ("Prefs/Env-Archive/AWeb3", "Prefs/Env-Archive/AWeb3"),
                 ("Prefs/Env-Archive/ClassAct", "Prefs/Env-Archive/ClassAct")),
        startup=("IF EXISTS SYS:Internet/AWeb",
                 "   Assign >NIL: AWEB_APL: SYS:Internet/AWeb",
                 "EndIF"),
    ),
    Package(
        "amftp", "AmFTP",
        "An FTP client, which is still how most Amiga file transfer is done.",
        category=Category.NETWORK,
        items=(("Internet/AmFTP", "Internet/AmFTP"),),
        requires=("mui",),
    ),
    Package(
        "wookiechat", "WookieChat",
        "An IRC client.",
        category=Category.NETWORK,
        items=(("Internet/WookieChat", "Internet/WookieChat"),),
        support=(("Libs/codesets.library", "Libs"),),
        requires=("mui",),
    ),
    Package(
        "ibrowse", "IBrowse",
        "The commercial browser. Only ever taken from a donor system you "
        "already own - it is not freely distributable.",
        category=Category.NETWORK,
        items=(("Internet/IBrowse", "Internet/IBrowse"),),
        support=(("Libs/openurl.library", "Libs"),),
        requires=("mui",),
    ),
]

CATALOGUE_BY_KEY = {p.key: p for p in CATALOGUE}


#  Where a package keeps drawer icons once its archive is unpacked, best
#  first.  MagicWB splits them: ImageDrawers holds the plain system drawers
#  (Storage, Tools, Utilities and so on), XEN-Icons the rest.
ICON_SET_DIRS = {
    "magicwb": ("MagicWB2.1p/XEN-Icons/SPECIAL/ImageDrawers",
                "MagicWB2.1p/XEN-Icons"),
}


def icon_set_dirs(key: str) -> list[Path]:
    """Unpacked directories of ``key`` that hold drawer icons.

    Only what is already in the cache is returned; this never downloads,
    because it runs while a volume is open and a build that got this far has
    already fetched whatever it is going to.
    """
    package = CATALOGUE_BY_KEY.get(key)
    if package is None or package.download is None:
        return []
    unpacked = cache_dir() / (package.download.filename.rsplit(".", 1)[0]
                              + ".unpacked")
    out = []
    for relative in ICON_SET_DIRS.get(key, ()):
        path = unpacked / relative
        if path.is_dir():
            out.append(path)
    return out


#  Libraries and devices Kickstart 3.1 has in ROM, or that a Workbench 3.1
#  install puts in LIBS: itself.  A program asking for one of these needs
#  nothing copied for it.
STOCK = {
    "exec", "dos", "graphics", "intuition", "layers", "utility", "expansion",
    "gadtools", "workbench", "icon", "keymap", "mathffp", "mathieeesingbas",
    "misc", "potgo", "timer", "input", "console", "trackdisk", "audio",
    "gameport", "keyboard", "ramdrive", "serial", "parallel", "printer",
    "clipboard", "translator", "diskfont", "commodities", "asl", "iffparse",
    "rexxsyslib", "rexxsupport", "mathtrans", "mathieeedoubbas",
    "mathieeedoubtrans", "mathieeesingtrans", "nonvolatile", "realtime",
    "bullet", "amigaguide", "datatypes", "locale", "lowlevel", "version",
    #  Workbench 3.1 installs these itself, into CLASSES:Gadgets.
    "colorwheel", "gradientslider", "tapedeck",
}

#  Never taken from a donor, whatever names it, because they are the CPU's own
#  support and SetPatch loads whichever the machine needs.  Scavenging them
#  broke every WHDLoad game: with mmu.library present a game dies on a yellow
#  screen, and the scan was quietly putting it back even after the package
#  that installs it had been deselected.  If someone wants these, the MMULib
#  package installs them deliberately and says what it costs.
NEVER_SCAVENGE = {
    "mmu", "memory", "softieee", "disassembler",
    "68020", "68030", "68040", "68060", "680x0",
    #  bsdsocket is not a library that lives in LIBS: at all - a TCP/IP stack
    #  puts it there while it runs.  Copying the donor's file leaves a stub
    #  with nothing behind it, and it stops every WHDLoad game: the game
    #  starts, the machine takes a CPU exception, and all you see is a yellow
    #  screen and then nothing.  Bisected to this one file, on its own,
    #  against a card proven to run the game.  The network package installs a
    #  stack, which is what actually provides it.
    "bsdsocket", "usergroup", "ixnet",
}

#  Where a donor system keeps the things programs look up by name, and where
#  each belongs on the card.  Order matters only in that the first hit wins.
LOOKUP_DIRS = (
    ("Libs", "Libs"),
    ("Classes", "Classes"),
    ("Classes/Gadgets", "Classes/Gadgets"),
    ("Classes/DataTypes", "Classes/DataTypes"),
    ("Devs", "Devs"),
    ("Devs/Networks", "Devs/Networks"),
    ("L", "L"),
    ("System/MUI/Libs", "System/MUI/Libs"),
    ("System/MUI/Libs/mui", "System/MUI/Libs/mui"),
)

REFERENCE = re.compile(
    rb"[A-Za-z0-9_]{2,28}\.(?:library|device|class|gadget|mcc)")

#  Small enough to skip nothing that matters: a stub library can be a few
#  hundred bytes, and a floor of 2 KB stepped straight over some of them.
#  Scanning a text file costs a little time and finds nothing, because a name
#  that is not a real library does not resolve in the donor anyway.
SCAN_MIN = 64
SCAN_MAX = 6 << 20


def _referenced(path: Path) -> set[str]:
    """Every library-ish name mentioned inside one file.

    Amiga binaries name what they open as plain strings, so reading them out
    is the only way to know what a program needs without a list maintained by
    hand - and a list maintained by hand is what kept missing things.  The
    pattern over-matches where two strings sit next to each other with no
    separator, which is harmless: a name that is really a fragment of another
    resolves to nothing in the donor and is dropped.
    """
    try:
        if not (SCAN_MIN <= path.stat().st_size <= SCAN_MAX):
            return set()
        data = path.read_bytes()
    except OSError:
        return set()
    return {m.decode("latin-1") for m in REFERENCE.findall(data)}


#  Where a system keeps the key files that register its software.
KEY_DIRS = (("S", "S"), ("L", "L"), ("Devs/keyfiles", "Devs/keyfiles"))


def _keys_for(names: Iterable[str], system: Path) -> list[tuple[str, str]]:
    """Key files belonging to things being copied.

    Registered Amiga software looks for ``<name>.key`` beside the system, not
    inside its own drawer: xadmaster.library wants ``S:xadmaster.key`` and
    telser.device wants ``S:telser.key``.  Copy the library and leave the key
    and it runs crippled or not at all, which looks like the copy having
    failed.  Matched on the stem, so a key is only taken when the thing it
    unlocks is going too.
    """
    stems = {Path(name).stem.lower() for name in names}
    out: list[tuple[str, str]] = []
    for folder, destination in KEY_DIRS:
        here = system / folder
        if not here.is_dir():
            continue
        try:
            entries = list(here.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or entry.suffix.lower() != ".key":
                continue
            if entry.stem.lower() in stems:
                out.append((str(entry), destination))
    return out


def _provided_by(pairs: Iterable[tuple[str, str]]) -> set[str]:
    """Every file name these copies will put on the card, lower-cased."""
    names: set[str] = set()
    for source, _destination in pairs:
        path = Path(source)
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    names.add(child.name.lower())
        elif path.is_file():
            names.add(path.name.lower())
    return names


def resolve_dependencies(pairs: list[tuple[str, str]],
                         donor: str | Path | None,
                         progress: Progress | None = None
                         ) -> list[tuple[str, str]]:
    """Extra files the copied programs need and the donor can supply.

    Declaring dependencies by hand caught MUI and a handful of libraries, and
    missed nineteen more: bsdsocket for the network clients, ixemul and
    netinfo for NetSurf, Picasso96API for AWeb, screennotify for Birdie,
    popupmenu and vapor_toolkit for the MUI applications.  Each one was a
    program that copied onto the card perfectly and then would not run.

    So they are read out of the binaries instead.  Anything a copied program
    names, that will not be on the card and that the donor has, is copied too.
    """
    system = donor_system(donor) if donor else None
    if system is None or not pairs:
        return []

    index: dict[str, tuple[Path, str]] = {}
    for folder, destination in LOOKUP_DIRS:
        here = system / folder
        if not here.is_dir():
            continue
        try:
            for entry in here.iterdir():
                if entry.is_file():
                    index.setdefault(entry.name.lower(), (entry, destination))
        except OSError:
            continue

    provided = _provided_by(pairs)

    def references_of(paths: Iterable[Path]) -> set[str]:
        names: set[str] = set()
        for path in paths:
            children = ([c for c in path.rglob("*") if c.is_file()]
                        if path.is_dir() else [path])
            for child in children:
                names |= _referenced(child)
        return names

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    frontier = [Path(source) for source, _destination in pairs]

    #  A library brings its own needs with it: mmu.library wants
    #  68030.library, ixemul wants ixnet, xpkmaster wants xfdmaster.  Resolving
    #  one round left seven of those behind, so keep going until a round finds
    #  nothing new.  It terminates because the donor holds finitely many files
    #  and none is ever taken twice.
    while frontier:
        found_now: list[tuple[str, str]] = []
        for name in sorted(references_of(frontier)):
            lowered = name.lower()
            stem = lowered.rsplit(".", 1)[0]
            if stem in STOCK or stem in NEVER_SCAVENGE or lowered in provided:
                continue
            if lowered in seen:
                continue
            found = index.get(lowered)
            if found is None:
                continue
            seen.add(lowered)
            found_now.append((str(found[0]), found[1]))
        out += found_now
        frontier = [Path(source) for source, _destination in found_now]

    #  Whatever is going, take the key that registers it.  Deduplicated
    #  against what is already being copied as well as what was resolved: the
    #  writer creates files and refuses to overwrite, so a second copy of one
    #  key would end the build.
    taken = {Path(source).name for source, _d in pairs}
    taken |= {Path(source).name for source, _d in out}
    already = {source for source, _d in pairs} | {source for source, _d in out}
    for pair in _keys_for(taken, system):
        if pair[0] not in already:
            already.add(pair[0])
            out.append(pair)
    if out and progress is not None:
        progress.log(f"  {len(out)} further file(s) the chosen software needs "
                     f"were found in the donor system")
        for source, destination in out:
            progress.log(f"    {Path(source).name} -> {destination}")
    return out


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


# --------------------------------------------------------------- donors

def donor_system(folder: str | Path) -> Path | None:
    """Find a Workbench system drive to copy packages out of.

    Accepts the drive itself, or a PiMiga folder, in which case its System
    drive is used.
    """
    folder = Path(folder)
    for candidate in (folder, folder / "System", folder / "disks" / "System",
                      folder / "pimiga" / "disks" / "System"):
        #  A C drawer is what makes something a system drive; whether it holds
        #  any particular package is checked per package afterwards.
        if (candidate / "C").is_dir():
            return candidate
    return None


def available(donor: str | Path | None) -> dict[str, list[str]]:
    """Which packages this donor can supply, and what each is missing."""
    system = donor_system(donor) if donor else None
    if system is None:
        return {}
    found: dict[str, list[str]] = {}
    for package in CATALOGUE:
        if not package.items:
            continue
        missing = [source for source, _dest in package.items
                   if not (system / source).exists()]
        #  A package is offered when at least its first item is there; the rest
        #  are extras that some installations arrange differently.
        if len(missing) < len(package.items):
            found[package.key] = missing
    return found


def obtainable(donor: str | Path | None) -> set[str]:
    """Every package that could be installed, from a donor or from Aminet."""
    keys = set(available(donor))
    keys.update(p.key for p in CATALOGUE if p.download)
    return keys


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
    if target.exists() and target.stat().st_size:
        progress.log(f"  {package.label}: using cached "
                     f"{target.name} ({human_size(target.stat().st_size)})")
        return target
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
    except Exception as error:                    # noqa: BLE001 - reported
        temporary.unlink(missing_ok=True)
        progress.log(f"  {package.label}: download failed ({error}), skipped")
        return None
    temporary.replace(target)
    progress.log(f"  {package.label}: {human_size(target.stat().st_size)}")
    return target


def unpack(archive: Path, progress: Progress) -> Path | None:
    """Unpack an LhA archive into the cache, once, and return the directory."""
    destination = cache_dir() / (archive.stem + ".unpacked")
    if destination.is_dir() and any(destination.iterdir()):
        return destination
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
    for name, destination, text in package.download.write:
        made = cache_dir() / (package.key + "-written") / destination
        made.mkdir(parents=True, exist_ok=True)
        (made / name).write_text(text)
        pairs.append((str(made / name), destination))
        progress.log(f"  {package.label}: wrote {destination}/{name}")
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
        return _merged(package, root, progress)
    if not download.items:
        #  Self-installing: put the whole thing on the card to run there.
        inner = [p for p in root.iterdir() if p.is_dir()]
        source = inner[0] if len(inner) == 1 else root
        return [(str(source), download.stage)]
    out: list[tuple[str, str]] = []
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


def overlays_for(donor: str | Path | None, keys: list[str],
                 rtg: bool = True,
                 chipset: Chipset = Chipset.AGA,
                 display: Display | None = None,
                 progress: Progress | None = None,
                 allow_download: bool = False) -> list[tuple[str, str]]:
    """Turn chosen packages into (source, destination) pairs to copy.

    A donor is preferred over a download: it is already on this machine, and
    for the packages that are not freely distributable it is the only route.
    """
    if display is None:
        display = Display.RTG_HDMI if rtg else Display.NATIVE
    progress = progress or Progress()
    system = donor_system(donor) if donor else None
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

    for key in expand(keys):
        package = CATALOGUE_BY_KEY.get(key)
        if package is None or not package.suits(chipset, display):
            continue
        from_donor: list[tuple[str, str]] = []
        if system is not None:
            for source, destination in package.items:
                path = system / source
                if path.exists():
                    from_donor.append((str(path), destination))
        if from_donor:
            add(from_donor)
        elif allow_download and package.download is not None:
            add(fetch(package, progress))
        #  Support goes on whichever way the package itself arrived.
        if system is not None:
            for source, destination in package.support:
                path = system / source
                if path.exists():
                    add([(str(path), destination)])
                elif progress is not None:
                    progress.log(f"  {package.label}: {source} is not in the "
                                 f"donor system; it may not run without it")
    return out


def default_keys(rtg: bool = True) -> list[str]:
    return [p.key for p in CATALOGUE if p.default and (rtg or not p.rtg_only)]


def suggested(machine: Machine, display: Display, *,
              donor: str | Path | None = None,
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
      and a locked palette earn their place, and MagicWB's eight colours suit
      it better than icons that assume a deep display.
    * On an RTG screen there is no blitter in the way; Picasso96 is the point
      of it, and a heavier desktop becomes affordable.
    """
    chosen = ["whdload", "lha", "installer", "igame", "iconlib",
              "magicmenu", "visualprefs"]
    if display.uses_native and machine.chipset is not Chipset.NONE:
        chosen += ["fblit", "ftext", "fullpalette", "magicwb"]
    if display.uses_rtg:
        chosen += ["picasso96"]
    if machine.aga or display.uses_rtg:
        chosen += ["scalos"]
    if networking:
        chosen += ["network", "amissl", "netsurf"]
    possible = obtainable(donor)
    return [key for key in chosen
            if key in possible and suits(key, machine.chipset, display)]
