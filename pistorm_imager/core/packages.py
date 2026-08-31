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
import shutil
import subprocess
import urllib.request
from pathlib import Path

from .machines import Chipset, Display, Machine
from .util import Progress, human_size

AMINET = "https://aminet.net/"
USER_AGENT = "pistorm-imager"


class Category(enum.Enum):
    """How the packages are grouped when they are offered."""

    SYSTEM = "System"
    LOOK = "Look and feel"
    SPEED = "Speed"
    NETWORK = "Networking"


@dataclasses.dataclass(frozen=True)
class Download:
    """A freely distributable archive on Aminet.

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

    @property
    def url(self) -> str:
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
    note: str = ""

    @property
    def manual(self) -> bool:
        """Whether this one has to finish installing on the Amiga itself."""
        return bool(self.download and not self.download.items)

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
        download=Download("util/libs/IconLib_46.4.lha",
                          (("IconLib_46.4/Libs/icon.library", "Libs"),
                           ("IconLib_46.4/ThirdParty/LoadResident/LoadResident",
                            "C"))),
        startup=("C:LoadResident LIBS:icon.library",),
        note="Soft-kicked over the ROM's own icon.library at boot, so it "
             "takes effect from the second start onwards - the first boot "
             "still draws with the 40.1 in ROM.",
    ),
    Package(
        "magicwb", "MagicWB",
        "The classic eight-colour icon and font set. Designed for exactly the "
        "kind of limited palette a native Workbench has.",
        category=Category.LOOK,
        download=Download("util/wb/MagicWB21p.lha",
                          stage=STAGING + "/MagicWB"),
        note="Icons, fonts and patterns to apply from Storage/Install.",
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
        note="Run its Installer from Storage/Install on the Amiga; Emu68 "
             "supplies the VideoCore board driver itself.",
    ),

    # -------------------------------------------------------- networking
    Package(
        "network", "TCP/IP networking",
        "The PiStorm's own network device and a TCP/IP stack, which is what "
        "everything else here needs. Comes from a donor: the stack PiMiga "
        "ships is already configured for the PiStorm.",
        category=Category.NETWORK,
        items=(("Devs/Networks/vlink.device", "Devs/Networks"),
               ("AmiTCP", "AmiTCP"),
               ("Internet/Genesis", "Internet/Genesis")),
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
        note="Unpacked into Internet/NetSurf, ready to run.",
    ),
    Package(
        "aweb", "AWeb",
        "The lighter classic browser, now freely distributable. Quicker than "
        "NetSurf on a plain native screen.",
        category=Category.NETWORK,
        items=(("Internet/AWeb_APL", "Internet/AWeb"),),
    ),
    Package(
        "amftp", "AmFTP",
        "An FTP client, which is still how most Amiga file transfer is done.",
        category=Category.NETWORK,
        items=(("Internet/AmFTP", "Internet/AmFTP"),),
    ),
    Package(
        "wookiechat", "WookieChat",
        "An IRC client.",
        category=Category.NETWORK,
        items=(("Internet/WookieChat", "Internet/WookieChat"),),
    ),
    Package(
        "ibrowse", "IBrowse",
        "The commercial browser. Only ever taken from a donor system you "
        "already own - it is not freely distributable.",
        category=Category.NETWORK,
        items=(("Internet/IBrowse", "Internet/IBrowse"),),
    ),
]

CATALOGUE_BY_KEY = {p.key: p for p in CATALOGUE}


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
    for key in keys:
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
            out += from_donor
        elif allow_download and package.download is not None:
            out += fetch(package, progress)
    return out


def default_keys(rtg: bool = True) -> list[str]:
    return [p.key for p in CATALOGUE if p.default and (rtg or not p.rtg_only)]


def suggested(machine: Machine, display: Display, *,
              donor: str | Path | None = None,
              networking: bool = False) -> list[str]:
    """A sensible set for this machine and this screen.

    The reasoning, in one place rather than scattered through the interface:

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
