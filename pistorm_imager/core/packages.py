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

from . import amigainfo
from .compat import EMU68_BOARD
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
    #  (icon inside the archive, destination, name on the card, DefaultTool).
    #  A project icon runs its DefaultTool on the file beside it, which is
    #  how a script becomes something that can be double clicked. Borrowing
    #  an icon the archive already has and retargeting it beats inventing
    #  one: a hand-built DiskObject with no image draws as nothing at all.
    retool: tuple[tuple[str, str, str, str], ...] = ()
    #  (icon inside the archive, destination, name on the card, tool types to
    #  set, each written "KEY=value"). An icon can carry settings the program
    #  reads, and which of them is right is decided by the machine being built
    #  rather than by the archive.
    #
    #  Picasso96 is the case: it finds its graphics board through the BOARDTYPE
    #  tool type of its monitor icon and then opens LIBS:Picasso96/<that>.card.
    #  The archive ships that icon with no tool types at all - because its own
    #  installer asks which board you have and writes one - so an untouched copy
    #  left Picasso96 with no idea which card to load. It guesses, by scanning
    #  for an autoconfig board, and Emu68's VideoCore is not one: it is found
    #  through the Pi's device tree. So the guess failed and the boot said
    #  "Could not create graphics board context for 'Picasso96'".
    tooltypes: tuple[tuple[str, str, str, tuple[str, ...]], ...] = ()

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
    #  Recommended: ticked wherever it suits the machine and the screen.  This
    #  is the *only* statement of what a sensible card carries.  There used to
    #  be a second one - a hand-written list of keys inside ``suggested()`` -
    #  and the two disagreed about nine packages, so the tick boxes and the
    #  "suggest a set" button recommended different cards.
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
    #  Data files a startup line has to name, which only the archive can
    #  decide: ``(placeholder, destination drawer, how many)``.  The line
    #  carries ``{placeholder}`` and the build replaces it with the files the
    #  package actually put in that drawer, in name order, as ``SYS:`` paths.
    #
    #  Birdie is the case this exists for and it is worth writing down, because
    #  the failure was silent and looked like the software working.  Birdie
    #  takes the window-border patterns to use on its command line, and it was
    #  started with none: run with an empty PATTERNS list it opens a window
    #  titled "About Birdie 2000" instead - so every boot ended with an about
    #  box on the desktop and no patterns anywhere.  The names cannot be
    #  written into the catalogue, because the archive decides what patterns it
    #  ships, so they are read back off what was installed.
    #
    #  A line whose placeholder cannot be filled is dropped rather than written
    #  bare: an unfilled line is the very thing that opened the about box.
    startup_files: tuple[str, str, int] = ()
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
    #  A library that cannot wait for S:User-Startup.  It has to be soft-kicked
    #  from S:Startup-Sequence before IPrefs opens the ROM's copy, and a drive
    #  that brings its own boot script has nowhere to put that line - so the
    #  package is left out rather than installed where something opens it at
    #  the wrong moment.  Naming the library here keeps the rule with the
    #  package it belongs to instead of in a table in the builder.
    boot_library: str = ""
    #  Installed once for every drive this build fills with content, so a card
    #  with a Games drive and a Demos drive arrives with a launcher for each.
    per_content_drive: bool = False
    #  The preferences file inside such a launcher that names the drawers it
    #  scans, one ``Volume:drawer`` per line.  The name and the format belong
    #  to the launcher, so they are declared with it.
    content_list: str = ""
    #  Words in a filled drive's name or folder that mean this package is
    #  about what is on it.  Nothing names a drive here: the words are what to
    #  look for in whatever drives the user set up.
    content_words: tuple[str, ...] = ()
    #  Warn when a drive matching ``content_words`` is being filled and this is
    #  not installed - the content then has nothing able to launch it.
    needed_for_content: bool = False
    #  Warn when this is installed and no drive matches ``content_words`` - it
    #  will open on an empty list.
    wants_content: bool = False
    #  Paths that prove this software is already on a drive being imported.
    #  Used to describe what an image brings, so the description follows the
    #  catalogue rather than a second list that has to be kept level with it.
    evidence: tuple[str, ...] = ()

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


MOUNT_ADF_SCRIPT = '.key NAME/F\n;\n; MountADF - choose a disk image and mount it as a floppy drive.\n;\n; Written by the PiStorm imager. Double click it and it asks for the file,\n; or pass one:  Execute SYS:Utilities/ADF_Device/MountADF <file>.adf\n; Either way it hands the job to the ADF Device\'s own Insert.script, which\n; asks which unit, mounts it if it is not mounted, and tells DOS the disk\n; has changed - after which AD0: is on Workbench like any other floppy.\n;\nIF "<NAME>" EQ ""\n  RequestFile >ENV:PiStormADF TITLE "Choose a disk image to mount" PATTERN "#?.adf" NOICONS\n  IF EXISTS ENV:PiStormADF\n    IF NOT "$PiStormADF" EQ ""\n      Execute SYS:Utilities/ADF_Device/Insert.script $PiStormADF\n    ENDIF\n    Delete >NIL: ENV:PiStormADF\n  ENDIF\nELSE\n  Execute SYS:Utilities/ADF_Device/Insert.script <NAME>\nENDIF\n'


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
        content_words=("game", "demo", "whdload"),
        needed_for_content=True,
        evidence=("C/WHDLoad",),
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
            #  on a PiStorm they stop iGame launching anything: it lists the
            #  games and does nothing when one is clicked.  iGame's own site
            #  names these two, and no_guigfx=1 with the libraries left off
            #  is the fix.  They are optional, so the card does without them.
            #
            #  This used to say the cause was the 153 floating point
            #  instructions in render.library meeting a 68040 with no FPU.
            #  That is wrong twice over: Emu68 provides an FPU unless the
            #  card is booted with "nofpu", which this tool never writes, and
            #  every one of those instructions is a 68040 on-chip operation -
            #  not one 68881 transcendental needing a trap the emulator might
            #  not service.  The fix stands on what was observed; the reason
            #  is unestablished.  See the README.
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
        default=True,
        per_content_drive=True,
        content_words=("game",),
        wants_content=True,
        content_list="repos.prefs",
    ),
    Package(
        "identify", "identify.library",
        "Lets tools name the hardware they are running on. A dependency of "
        "several of the others.",
        #  IdentifyUsr, not Identify. Aminet still carries the 1997 upload
        #  under the shorter name and it is version 8.2; the author's current
        #  release is 45.1, dated August 2025, and lives here. The archive
        #  ships a 68000 build beside it - identify.library_000 - which is not
        #  what a PiStorm is.
        download=Download("util/libs/IdentifyUsr.lha",
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
        default=True,
        boot_library="icon.library",
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
        default=True,
    ),
    Package(
        "visualprefs", "VisualPrefs",
        "Redraws window borders and gadgets - thin, flat, modern instead of "
        "the stock bevels. Patches the GUI, so it installs itself on the Amiga.",
        category=Category.LOOK,
        download=Download("util/wb/VisualPrefs.lha",
                          stage=STAGING + "/VisualPrefs"),
        note="Run its Installer from Storage/Install on the Amiga.",
        default=True,
    ),
    Package(
        "fullpalette", "FullPalette",
        "Locks down the Workbench palette so that opening a program cannot "
        "scramble your desktop colours. Matters most on a native screen.",
        category=Category.LOOK,
        native_only=True,
        download=Download("util/wb/FullPalette22.lha",
                          #  These two are the other way round from how they
                          #  read. The archive's own installer says it plainly:
                          #  "FPPrefs (the FullPalette daemon that is run in
                          #  the Startup-sequence)". FullPalette is the
                          #  *editor* - its strings are Palette Preferences,
                          #  Load, Save - and putting it in WBStartup opened
                          #  the palette editor on every single boot.
                          (("FullPalette/FPPrefs", "WBStartup"),
                           ("FullPalette/FullPalette", "Prefs"),
                           ("FullPalette/FullPalette.info", "Prefs")),
                          #  The daemon has no icon of its own in the
                          #  archive, and a program in WBStartup without one
                          #  is never started at all - so it borrows the
                          #  editor's under its own name.
                          rename=(("FullPalette/FullPalette.info",
                                   "WBStartup", "FPPrefs.info"),)),
        default=True,
        chipsets=(Chipset.OCS, Chipset.ECS, Chipset.AGA),
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
        #  "Run", not "C:Run". Run is one of the shell's ROM-internal
        #  commands and is not a file at all: AmigaOS 3.1 ships no C:Run, so
        #  the pathed form failed on every boot and Birdie never started.
        #  ClassicWB's own User-Startup says "Run >NIL: C:XpkMasterPrefs",
        #  which is the form that works.
        #
        #  The patterns have to be named on the line. Started without them
        #  Birdie draws nothing and opens its "About Birdie 2000" window
        #  instead, which is what greeted the user on every boot; the names
        #  come from what was installed rather than from here, because the
        #  archive decides what patterns it ships.
        startup=("Run >NIL: C:Birdie {patterns}",),
        #  One, not all seven. Birdie keeps each pattern in three versions -
        #  plain, shine and shadow - so handing it the whole drawer costs
        #  memory on a machine that has little, and gives every window a
        #  pattern picked at random, which is a patchwork rather than a look.
        startup_files=("patterns", "Prefs/Presets/Birdie", 1),
        note="Installed into C: with its patterns in Prefs/Presets/Birdie, "
             "and started from S:User-Startup with the first of them. The "
             "patterns are JPEGs and are loaded through datatypes, so a "
             "system with no JPEG datatype simply gets plain borders.",
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
        #  Aminet still carries a patch for a guru in 4.0, which makes the
        #  package look unsafe at a glance. Its own history records the fix
        #  twice over - "68040 non FPU guru fixed" in 4.3 and "68040/68060
        #  non FPU guru fixed, again!" in 4.4 - and 4.4 is what this address
        #  serves, so the patch is not needed. (This note used to add that a
        #  PiStorm is the FPU-less 68040 that bug needs. It is not; taking
        #  the current release is the right choice regardless.)
        download=Download("util/moni/SysInfo.lha", stage="Utilities/SysInfo"),
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
        "adfdevice", "ADF Device",
        "Mount an .adf file as a floppy drive and read it like a disk, "
        "without writing it to real media. Insert one, and AD0: appears on "
        "Workbench.",
        category=Category.EXTRAS,
        download=Download(
            "disk/misc/ADF_Device.lha",
            #  adf.device and its mountlist have to be in DEVS: together: the
            #  scripts mount "from devs:adf.ml", and the device is what that
            #  mountlist names.
            (("ADF_Device_v1.3/adf.device", "Devs"),
             ("ADF_Device_v1.3/ADF.ml", "Devs"),
             ("ADF_Device_v1.3/InsertDisk", "C"),
             ("ADF_Device_v1.3/RemoveDisk", "C"),
             ("ADF_Device_v1.3/Insert.script", "Utilities/ADF_Device"),
             ("ADF_Device_v1.3/Remove.script", "Utilities/ADF_Device"),
             ("ADF_Device_v1.3/ADF_Device.guide", "Utilities/ADF_Device"),
             ("ADF_Device_v1.3/ADF_Device.guide.info",
              "Utilities/ADF_Device")),
            #  The archive has no Workbench front end at all: its own scripts
            #  want a Shell and a filename. This one asks for the file with
            #  RequestFile and then hands over to theirs, so it works by
            #  double click - and it is part of this package, not a loose
            #  extra, because it is no use without the device beside it.
            write=(("MountADF", "Utilities/ADF_Device", MOUNT_ADF_SCRIPT),),
            #  ...and the icon that makes double clicking it run it. IconX is
            #  Workbench's script runner; the guide's own icon is borrowed
            #  and retargeted, because an invented one would have no image.
            retool=(("ADF_Device_v1.3/ADF_Device.guide.info",
                     "Utilities/ADF_Device", "MountADF.info", "IconX"),)),
        note="Bjoern Fuglsang's adf.device goes into DEVS: with its "
             "mountlist, and Utilities/ADF_Device holds MountADF - double "
             "click it, pick an .adf, pick a unit, and the disk appears on "
             "Workbench as AD0:. Sixteen units are mountable.",
    ),
    Package(
        "reqtools", "ReqTools",
        "The file and font requesters a great deal of Amiga software asks "
        "for by name. Nothing shows without it.",
        category=Category.SYSTEM,
        download=Download("util/libs/ReqToolsUsr.lha",
                          (("ReqTools/libs/reqtools.library", "Libs"),)),
        #  Fetched rather than assumed. Most prepared drives carry a copy,
        #  but a card must not be built out of what the source image happened
        #  to hold: build on a drive without one and the virus killer opens
        #  no requester and looks broken.
        support_only=True,
        note="Installed into LIBS: for the software that needs it.",
    ),
    Package(
        "xvs", "xvs.library",
        "The virus recognition and disinfection engine every current Amiga "
        "virus killer shares. This is the part that has to be recent.",
        category=Category.SYSTEM,
        download=Download("util/virus/xvslibrary.lha",
                          (("xvs/libs/xvs.library", "Libs"),)),
        support_only=True,
        note="Version 33.49, published April 2025 - the most recently "
             "updated piece of Amiga software on this card.",
    ),
    Package(
        "virusz", "VirusZ III",
        "The virus killer. Checks memory, boot blocks, files and the "
        "contents of archives, and is the last one still being maintained.",
        category=Category.EXTRAS,
        download=Download(
            "util/virus/VirusZ.lha",
            #  Placed file by file rather than unpacked whole: the archive
            #  also carries a MorphOS icon and PGP signatures, which are not
            #  wanted on the card.
            (("VirusZ/VirusZ", "Utilities/VirusZ"),
             ("VirusZ/VirusZ.info", "Utilities/VirusZ"),
             ("VirusZ/VirusZ.doc", "Utilities/VirusZ"),
             ("VirusZ/VirusZ.doc.info", "Utilities/VirusZ"),
             #  The archive's own top level icon, which becomes the drawer's.
             ("VirusZ.info", "Utilities")),
        ),
        #  The scanner is not in the program: it is in xvs.library, which is
        #  why that is updated on its own and why this is worth having at all.
        #  ReqTools is what it opens its file requester with.
        requires=("xvs", "reqtools"),
        note="Version 1.04, installed into Utilities/VirusZ and ready to "
             "run. Not started at boot: it is a checker to reach for, and a "
             "resident memory watcher costs a card that has no memory to "
             "spare. Its scanner, xvs.library, comes with it.",
    ),
    Package(
        "ahi", "AHI",
        "The Amiga's standard audio interface. Programs ask AHI for sound "
        "instead of driving Paula themselves, so they share the hardware "
        "rather than fighting over it, and a stock machine gets 14-bit "
        "output instead of 8.",
        category=Category.MEDIA,
        #  Only the prefs program needs it, but it is the only way to choose
        #  a mode afterwards. The BGUI build would avoid the dependency; it
        #  was passed over because its bgui.library carries floating point
        #  instructions, reasoning that no longer holds now that a PiStorm is
        #  known to have an FPU. The choice is unaffected - MUI is on the
        #  card anyway - so it is left alone rather than churned.
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
        "AGA or an RTG screen, a poor trade on a plain OCS desktop. Aminet's "
        "copy is 1.2b from 2000, which is old - the maintained Scalos is not "
        "on Aminet.",
        category=Category.LOOK,
        #  Aminet's is 1.2b from April 2000 and is the newest there under
        #  that name; the maintained Scalos lives elsewhere. Said plainly so
        #  nobody assumes ticking this brings a current one.
        download=Download("util/wb/Scalos.lha", stage=STAGING + "/Scalos"),
        chipsets=(Chipset.AGA,),
        or_rtg=True,
        #  One of the few things that genuinely cannot be installed from here:
        #  its script picks between three builds of every module for the
        #  machine it finds, and replacing the desktop half-way is how a card
        #  stops booting.
        note="Unpacked into Storage/Install/Scalos - staged, not installed. "
             "Run its Install.Scalos on the Amiga: it chooses between three "
             "builds of each module for the machine it finds, which cannot "
             "be decided from here. Check the version first if your drive "
             "already carries Scalos - Aminet's is 1.2b from February 2000, "
             "and ClassicWB FULL ships 1.2d from January 2001, so running "
             "this installer over it is a step backwards.",
        default=True,
        evidence=("C/Scalos",),
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
        default=True,
        chipsets=(Chipset.OCS, Chipset.ECS, Chipset.AGA),
    ),
    Package(
        "ftext", "FText",
        "The companion to FBlit that does the same for text rendering.",
        category=Category.SPEED,
        native_only=True,
        download=Download("util/boot/FText.lha", (("FText", "C"),)),
        startup=("C:FText >NIL:",),
        default=True,
        chipsets=(Chipset.OCS, Chipset.ECS, Chipset.AGA),
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
             #  rtg.library is the RTG subsystem itself, and it was missing.
             #  DEVS:Monitors/Picasso96 is run by S:Startup-Sequence and the
             #  string inside that binary is "picasso96/rtg.library", opened
             #  relative to LIBS: - so a card with the monitor, the settings
             #  and the board driver but no rtg.library asked for a library
             #  that was not there on every boot, and had no RTG screen.
             #  Only fastlayers was being copied out of this drawer; the
             #  archive's own installer copylibs all three of these to
             #  SYS:Libs/Picasso96, unconditionally.
             ("Picasso96Install/Libs/Picasso96/rtg.library",
              "Libs/Picasso96"),
             ("Picasso96Install/Libs/Picasso96/fastlayers.library",
              "Libs/Picasso96"),
             ("Picasso96Install/Libs/Picasso96/emulation.library",
              "Libs/Picasso96"),
             ("Picasso96Install/Devs/Monitors/Picasso96", "Devs/Monitors"),
             ("Picasso96Install/Prefs/Picasso96Mode", "Prefs"),
             ("Picasso96Install/Prefs/Picasso96Mode.info", "Prefs")),
            #  The archive ships one settings file per monitor frequency and
            #  its installer asks which. Emu68's output is HDMI, so the most
            #  permissive of them is the one that does not needlessly cut the
            #  mode list short.
            rename=(("Picasso96Install/Devs/Picasso96Settings.64", "Devs",
                     "Picasso96Settings"),),
            #  The monitor's icon is how Picasso96 is told which board to
            #  drive, and the archive ships it blank. Emu68's VideoCore is not
            #  an autoconfig board, so nothing can find it by looking; it has
            #  to be named.
            tooltypes=(("Picasso96Install/Devs/Monitors/Picasso96.info",
                        "Devs/Monitors", "Picasso96.info",
                        (f"BOARDTYPE={EMU68_BOARD}",)),)),
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
        default=True,
        evidence=("Libs/Picasso96",),
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
        default=True,
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
        default=True,
    ),
    Package(
        "amissl", "AmiSSL",
        "TLS for the Amiga. Without it almost nothing on the modern web will "
        "answer.",
        category=Category.NETWORK,
        download=Download("util/libs/AmiSSL-v5-OS3.lha",
                          stage=STAGING + "/AmiSSL"),
        note="Run its Installer from Storage/Install on the Amiga.",
        default=True,
    ),
    Package(
        "netsurf", "NetSurf",
        "A browser that renders modern HTML and CSS, and the most usable one "
        "on 68k hardware. It wants a big screen and plenty of memory, so on "
        "an OCS or ECS machine watching its own video, AWeb is the lighter "
        "choice.",
        category=Category.NETWORK,
        download=Download("comm/www/netsurf-m68k.lha",
                          stage="Internet/NetSurf"),
        requires=("mui",),
        note="Unpacked into Internet/NetSurf, ready to run.",
        default=True,
    ),
    Package(
        "aweb", "AWeb APL",
        "The browser an OCS or ECS machine can actually run. It draws on a "
        "plain Workbench screen in as little as 2 MB, where a modern renderer "
        "needs an RTG screen and much more of both.",
        category=Category.NETWORK,
        #  Installed, not staged. Its own Installer script does two things -
        #  copy this drawer, and add an "Assign AWEB_APL:" line to
        #  S:User-Startup - and both are done here, so the browser is ready to
        #  run rather than ready to install.
        #
        #  Programs/AWeb_APL is where its installer puts it by default, and
        #  ClassicWB's own User-Startup already assigns AWEB_APL: to exactly
        #  that path - so a card built on that distribution finds the browser
        #  the distribution was expecting, rather than a second copy elsewhere.
        download=Download("comm/www/aweb3.5.09_68k_20070721.lha",
                          #  The drawer's contents go into the drawer; its icon
                          #  goes beside it, or Workbench shows nothing there.
                          (("AWeb_APL", "Programs/AWeb_APL"),
                           ("AWeb_APL.info", "Programs"))),
        #  Guarded, and written whether or not the drive brought its own line:
        #  a card built from floppies has no such assign, and assigning twice
        #  to the same path costs nothing.
        startup=("IF EXISTS SYS:Programs/AWeb_APL",
                 "   Assign >NIL: AWEB_APL: SYS:Programs/AWeb_APL",
                 "EndIF"),
        note="Installed into Programs/AWeb_APL with its AWEB_APL: assign "
             "added to S:User-Startup, ready to run.",
        default=True,
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
            #  Handled with the file it belongs to, below. A stray icon whose
            #  file is not being placed is left behind deliberately.
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
    #  An icon travels with the file it belongs to.  Every top-level .info was
    #  being dropped here, which cost Roadshow the icon on Install_Roadshow -
    #  and an Installer script with no icon cannot be started from Workbench at
    #  all, so the package arrived staged and unreachable.
    pairs += _icons_for(pairs)
    pairs += _named_icons(package, pairs, progress)
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
    if not (download.items or download.rename or download.write
            or download.retool or download.tooltypes):
        inner = [p for p in root.iterdir() if p.is_dir()]
        source = inner[0] if len(inner) == 1 else root
        whole = [(str(source), download.stage)]
        return whole + _named_icons(package, whole, progress)
    out: list[tuple[str, str]] = _written(package, progress)
    for inside, destination, newname, entries in download.tooltypes:
        source = root / inside
        if not source.exists():
            progress.log(f"  {package.label}: {inside} is not in the archive")
            continue
        try:
            icon = source.read_bytes()
            for entry in entries:
                key, _, value = entry.partition("=")
                icon = amigainfo.set_tooltype(icon, key, value)
        except amigainfo.InfoError as error:
            #  Copied across untouched rather than left out: an icon with no
            #  tool types is what the archive shipped, and the program can
            #  still be pointed at its board by hand.
            progress.log(f"  {package.label}: could not set the tool types on "
                         f"{inside} ({error}); copied as it is")
            icon = source.read_bytes()
        staged = cache_dir() / f"{package.key}-tooltypes" / destination
        staged.mkdir(parents=True, exist_ok=True)
        (staged / newname).write_bytes(icon)
        out.append((str(staged / newname), destination))
        progress.log(f"  {package.label}: {newname} set to "
                     f"{', '.join(entries)}")
    for inside, destination, newname, tool in download.retool:
        source = root / inside
        if not source.exists():
            progress.log(f"  {package.label}: {inside} is not in the archive")
            continue
        try:
            icon = amigainfo.set_default_tool(source.read_bytes(), tool)
        except amigainfo.InfoError as error:
            progress.log(f"  {package.label}: {inside} is not an icon this "
                         f"understands ({error}); leaving it out rather than "
                         f"writing one that opens the wrong thing")
            continue
        staged = cache_dir() / f"{package.key}-retooled" / destination
        staged.mkdir(parents=True, exist_ok=True)
        (staged / newname).write_bytes(icon)
        out.append((str(staged / newname), destination))
        progress.log(f"  {package.label}: {newname} set to open with {tool}")
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
    #  Same rule as for a merged archive: whatever is placed, its icon goes
    #  with it. KingCON listed its Installation script and not
    #  Installation.info, so the script landed with no way to start it.
    out += _icons_for(out)
    out += _named_icons(package, out, progress)
    return out


#  How an Installer icon names the script it runs.  The convention is
#  Commodore's: the icon's DefaultTool is Installer and this tool type says
#  which script to feed it, so the icon need not be named after the script.
SCRIPT_TOOLTYPE = "SCRIPT="

#  An installer lives near the top of an archive; walking a whole Scalos tree
#  to look for one would cost more than it is worth.
ICON_SEARCH_DEPTH = 3


def _named_icons(package: Package, pairs: list[tuple[str, str]],
                 progress: Progress) -> list[tuple[str, str]]:
    """Give a script the icon that names it but is not named after it.

    Several archives ship their installer as a script with no icon of its own,
    beside an icon with no file of its own - ``MCP-Install.english.info`` says
    ``SCRIPT=Install_MCP``, ``Scalos-Install.english.info`` says
    ``SCRIPT=Install.Scalos``, ``Setup.info`` says ``SCRIPT=InstallPicasso96``.

    Workbench shows an icon only when the file beside it exists, so both halves
    are invisible: the script cannot be started and the icon is not drawn.  The
    card then carries a package that says "run its Installer on the Amiga" and
    no way to do it short of a Shell.

    So the icon is placed a second time under the script's name.  The original
    is left where it is - it is what the archive shipped - and nothing is
    invented: these are the archive's own icons, saying themselves which script
    they belong to.
    """
    out: list[tuple[str, str]] = []
    done: set[Path] = set()
    for source, destination in pairs:
        root = Path(source)
        if not root.is_dir():
            continue
        try:
            #  An archive can ship one installer icon per language - MCP has a
            #  deutsch and an english - and they differ only in the LANGUAGE
            #  tool type that Installer reads. Sorted order alone would have
            #  handed a German installer to an English tool, so the language
            #  is chosen rather than fallen into.
            icons = sorted(root.rglob("*.info"),
                           key=lambda i: (0 if "english" in i.name.lower()
                                          else 1, str(i).lower()))
        except OSError:
            continue
        for icon in icons:
            here = icon.relative_to(root).parent
            if len(here.parts) >= ICON_SEARCH_DEPTH:
                continue
            #  An orphan: an icon whose own file is not there.
            if icon.with_name(icon.name[:-len(".info")]).exists():
                continue
            try:
                types = amigainfo.read_tooltypes(icon.read_bytes())
            except Exception:                    # noqa: BLE001 - an odd icon
                continue                         #  is simply not one of these
            named = next((entry[len(SCRIPT_TOOLTYPE):].strip() for entry in types
                          if entry.upper().startswith(SCRIPT_TOOLTYPE)), "")
            if not named:
                continue
            script = icon.parent / named
            if not script.is_file() or script.with_name(
                    script.name + ".info").exists():
                continue
            if script in done:
                continue
            done.add(script)
            where = f"{destination}/{here}".rstrip("/.") if here.parts \
                else destination
            staged = cache_dir() / f"{package.key}-iconnames" / where
            staged.mkdir(parents=True, exist_ok=True)
            target = staged / (named + ".info")
            target.write_bytes(icon.read_bytes())
            out.append((str(target), where))
            progress.log(f"  {package.label}: {named} given the icon from "
                         f"{icon.name}, so it can be started from Workbench")
    return out


def _icons_for(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The icons belonging to what is already being placed.

    On the Amiga a file is only visible in Workbench if its ``.info`` is beside
    it, and a drawer's icon lives in the *parent* of the drawer rather than
    inside it - so the two cases land in different places and both are needed.

    Only icons the archive already carries, and only for things being placed
    anyway: nothing is invented, and an icon whose file was left out stays left
    out.
    """
    already = {(source, destination) for source, destination in pairs}
    placed = {f"{destination}/{Path(source).name}".lower()
              for source, destination in pairs}
    out: list[tuple[str, str]] = []
    for source, destination in pairs:
        path = Path(source)
        if path.name.lower().endswith(".info"):
            continue
        icon = path.with_name(path.name + ".info")
        if not icon.is_file():
            continue
        #  A drawer placed *as* a destination - "AWeb_APL" copied to
        #  "Programs/AWeb_APL" - has its icon one level up, beside the drawer.
        where = (destination.rsplit("/", 1)[0] if path.is_dir()
                 and destination.lower().endswith("/" + path.name.lower())
                 else destination)
        pair = (str(icon), where)
        if pair in already or f"{where}/{icon.name}".lower() in placed:
            continue
        already.add(pair)
        out.append(pair)
    return out


#  Files an archive carries alongside its data but which are not data: an icon
#  is Workbench's business, not the program's, and naming one on a command line
#  would hand a program a file it cannot read.
NOT_DATA = (".info",)


def installed_names(package: Package,
                    pairs: Iterable[tuple[str, str]]) -> list[str]:
    """The data files ``package`` put in the drawer its startup line names.

    Read off the pairs the build resolved, not written down here: the archive
    decides what it ships, and a name typed into the catalogue would be a guess
    that goes wrong the first time its publisher adds or renames one.

    A pair's source is a whole drawer as often as a single file - the archive's
    ``Patterns`` drawer goes to ``Prefs/Presets/Birdie`` entire - so a directory
    is listed rather than named.
    """
    if not package.startup_files:
        return []
    _placeholder, drawer, limit = package.startup_files
    names: list[str] = []
    for source, destination in pairs:
        if destination.replace("\\", "/").lower() != drawer.lower():
            continue
        path = Path(source)
        try:
            found = ([path] if path.is_file()
                     else sorted(c for c in path.iterdir() if c.is_file()))
        except OSError:
            continue
        names += [c.name for c in found
                  if not c.name.lower().endswith(NOT_DATA)]
    names.sort(key=str.lower)
    return names[:limit] if limit else names


def complete_startup(package: Package,
                     pairs: Iterable[tuple[str, str]]) -> list[str] | None:
    """``package``'s startup lines with their placeholder filled in.

    ``None`` when the files it needs are not on the card, because the line
    must then not be written at all: Birdie started with no patterns opens its
    about window, which is worse than Birdie not being started.
    """
    if not package.startup_files:
        return list(package.startup)
    placeholder, drawer, _limit = package.startup_files
    names = installed_names(package, pairs)
    if not names:
        return None
    arguments = " ".join(f'"SYS:{drawer}/{name}"' for name in names)
    return [line.replace("{" + placeholder + "}", arguments)
            for line in package.startup]


#  How an archive marks which processor a binary is for: a suffix on the end
#  of the name, after a dot, an underscore or a hyphen. Matched as a whole
#  suffix so "iGame.030" is one and "AWeb.developer" is not.
CPU_SUFFIX = re.compile(
    r"(?i)^(?P<stem>.+)[._-](?:0[0-9]0|680[0-9]0|88[12]|fpu|nofpu|ppc|mos|os4)$")


def cpu_leftovers(package: Package,
                  pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Builds for other processors, left beside the one that was installed.

    An archive that ships one binary per processor is installed by ``rename``:
    the right one goes on under the name its icon launches. The archive's own
    drawer is usually copied as well, so the others land beside it - and on a
    card built for a 68040 that is ``iGame.030`` and ``iGame.060``, which
    nothing can run, next to ``iGame.040``, which is byte for byte the ``iGame``
    already there.

    Three copies of one program in a drawer, two of them for hardware the
    machine has not got and none of them clickable. Read off the ``rename``
    rather than named here, so it holds for whatever an archive calls them.
    """
    if not package.download or not package.download.rename:
        return {}
    out: dict[str, str] = {}
    for inside, destination, newname in package.download.rename:
        found = CPU_SUFFIX.match(Path(inside).name)
        if not found:
            continue
        stem = found.group("stem").lower()
        #  Everything in the same drawer whose name is that stem with a
        #  processor on the end - the installed one included, because it is
        #  now on the card under the name the icon uses.
        for source, where in pairs:
            here = Path(source)
            names = ([here] if here.is_file()
                     else sorted(here.iterdir()) if here.is_dir() else [])
            for item in names:
                other = CPU_SUFFIX.match(item.name)
                if other is None or other.group("stem").lower() != stem:
                    continue
                landing = f"{where}/{item.name}" if where else item.name
                out[landing] = (f"{newname} is installed as the one this "
                                f"machine runs")
    return out


#  What an icon's DefaultTool says when the file beside it is an Installer
#  script. Both spellings appear in the wild.
RUNS_THE_INSTALLER = ("installer", "c:installer", "sys:c/installer")


def redundant_installers(package: Package,
                         pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """A package's own Installer script, where the build has already installed it.

    An archive ships one so somebody can install it on the Amiga. When this
    tool stages the archive - into ``Storage/Install`` - that script is the
    point of the exercise and must stay. When the tool installs the software
    itself, the script sits in the finished drawer offering to do again what is
    already done: ``Programs/iGame/Install-iGame`` beside the iGame it just
    installed.

    Where it lands is the whole discriminator. Every one of these icons says
    ``DefaultTool=Installer``, staged or not, so the name and the icon cannot
    tell the two cases apart - but a script landing outside the staging drawer
    belongs to software that is already in place.
    """
    out: dict[str, str] = {}
    for source, destination in pairs:
        if destination.replace("\\", "/").lower().startswith(STAGING.lower()):
            continue                     # staged on purpose; the script is why
        here = Path(source)
        try:
            inside = ([here] if here.is_file()
                      else sorted(here.iterdir()) if here.is_dir() else [])
        except OSError:
            continue
        for icon in inside:
            if not icon.name.lower().endswith(".info"):
                continue
            script = icon.with_name(icon.name[:-len(".info")])
            if not script.is_file():
                continue
            try:
                tool = amigainfo.read_default_tool(icon.read_bytes())
            except Exception:            # noqa: BLE001 - not an icon we read
                continue
            if tool.strip().lower() not in RUNS_THE_INSTALLER:
                continue
            why = f"{package.label} is installed already, not staged"
            for name in (script.name, icon.name):
                out[f"{destination}/{name}" if destination else name] = why
    return out


# -------------------------------------------------------------- choosing

def suits(key: str, chipset: Chipset, display: Display) -> bool:
    package = CATALOGUE_BY_KEY.get(key)
    return package is not None and package.suits(chipset, display)


HUNK_HEADER = b"\x00\x00\x03\xf3"


def principal_programs(keys: list[str], progress: Progress | None = None,
                       **kw) -> tuple[dict[str, tuple[str, str, tuple | None]],
                                      set[str]]:
    """What the chosen packages install, by program name, and where they go.

    Nothing is written down in the catalogue: the names come from the
    archives themselves, so a duplicate can be looked for on any drive
    rather than only on the one distribution somebody checked by hand.

    Only *principal* programs - what a package puts at the top of a drawer,
    and only real AmigaDOS executables. A file buried three levels inside a
    package's tree is support, and its name is not the program's: matching on
    those turned a PFS3 tool into a duplicate of something inside Visage.
    """
    from . import content                                   # noqa: PLC0415
    wanted: dict[str, tuple[str, str, tuple | None]] = {}
    filling: set[str] = set()
    for key in expand(keys):
        package = CATALOGUE_BY_KEY.get(key)
        if package is None or package.download is None:
            continue
        for source, destination in overlays_for([key], progress=progress, **kw):
            path = Path(source)
            #  Only a *drawer* going onto the card fills its destination. A
            #  single file landing in one does not, and treating it that way
            #  put bare top-level drawers into this set - Programs, Utilities,
            #  Audio, System, Prefs, Storage, Libs, C, S, L, Devs, Locale and
            #  WBStartup, every one of them from some package dropping an icon
            #  beside its drawer. find_duplicates skips anything inside a
            #  drawer this build fills, so that suppressed duplicate detection
            #  almost everywhere a program lives: of a full package selection
            #  on ClassicWB only Tools/SysInfo was ever reported, and only
            #  because no package happens to put a file in Tools.
            if path.is_dir():
                filling.add(destination)
            try:
                candidates = ([path] if path.is_file()
                              else [c for c in path.iterdir() if c.is_file()])
            except OSError:
                continue
            for item in candidates:
                if not item.name:
                    continue
                try:
                    data = item.read_bytes()
                except OSError:
                    continue
                if data[:4] != HUNK_HEADER:
                    continue
                wanted.setdefault(item.name.lower(),
                                  (key, package.label,
                                   content.version_of(data)))
    return wanted, filling


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


def suggested(machine: Machine, display: Display, *,
              networking: bool = False) -> list[str]:
    """A sensible set for this machine and this screen.

    Nothing is listed here.  Every package says for itself whether it is
    recommended (``default``) and what it needs to be worth having
    (``rtg_only``, ``native_only``, ``chipsets``, ``or_rtg``), and this walks
    the catalogue applying those.  Adding a package to the recommended set is
    then one word on the package, and it cannot fall out of step with the tick
    boxes on the packages page, which read the same field.

    That mattered.  This used to be a hand-written list of keys, and it had
    drifted from the ``default`` flags in both directions: DefIcons and
    FreeWheel were ticked on a fresh window but never suggested, while iGame,
    the icon library, MagicMenu, VisualPrefs, FBlit, FText, FullPalette,
    Picasso96 and Scalos were suggested but never ticked.  Two answers to
    "what should this card carry?", disagreeing about nine packages.

    The reasoning behind the flags, which is the part worth writing down:

    * The CPU patches are deliberately NOT recommended.  Newer SetPatch and
      CPU libraries look like an obvious win on a 68040-class machine, and
      they stop every WHDLoad game from running - which is what these cards
      are mostly for.
    * On a native screen the cost is the chipset drawing it, so FBlit, FText
      and a locked palette earn their place - and none of them mean anything
      on a bare Pi with no Amiga chipset to patch, which is why they name the
      chipsets they want rather than being special-cased here.
    * On an RTG screen there is no blitter in the way; Picasso96 is the point
      of it, and a heavier desktop becomes affordable.
    * Networking is off unless it was asked for, so nothing in the network
      category is recommended until there is a network to use.  The extras in
      it - an FTP client, an IRC client - stay off even then: they are a
      preference, not part of getting online.
    """
    return [p.key for p in CATALOGUE
            if p.default
            and not p.support_only              # arrives via ``requires``
            and (networking or p.category is not Category.NETWORK)
            and p.suits(machine.chipset, display)]
