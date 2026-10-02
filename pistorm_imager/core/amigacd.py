"""Installing AmigaOS 3.2, 3.5 and 3.9 from their CD images.

These releases were sold on CD, and a CD is a source of its own rather than
another folder of floppies.  The 3.5 and 3.9 discs hold the system already laid
out as directory trees.  The 3.2 disc holds it as floppy images - thirty-five
of them under ``ADF`` - which its installer mounts one after another.  Both
shapes come to the same thing here: a release is a list of layers, each a tree
to copy and a place to put it, and a layer may name a tree on the disc or a
tree inside a floppy image on the disc.

**Where the layout comes from.**  Every source and destination below was read
out of the installer script each disc carries - ``OS-Version3.5/OS3.5Install``
and ``OS-Version3.9/OS3.9Install``, and ``Install/Install`` on the 3.2 disc's
``Install3.2`` floppy - which are plain Installer text.  That
matters more than it sounds: several of the destinations are not the obvious
ones, and guessing them would produce a system that looks installed and is
subtly wrong.  ``Extras/Backdrops`` goes to ``Prefs/Presets/Backdrops`` and not
to ``Backdrops``; the printer and keymap sets are lifted out of the Workbench
tree's own ``Storage`` and copied *again* into ``Devs``; and 3.2's boot script
is not the one on its Workbench disk but ``Update/Startup-HardDrive`` from the
Install disk, renamed.

**Why the order matters.**  No release is self-contained, and the discs
are not layered the same way:

* The **3.9 disc** carries ``Workbench3.5`` and ``Workbench3.9`` side by side.
  ``Workbench3.5`` there is a complete system; ``Workbench3.9`` is the overlay
  that turns it into 3.9.  Its own installer copies both, in that order, for a
  3.9 install.
* The **3.5 disc** carries a *delta*.  ``OS-Version3.5/Workbench`` has no ``S``,
  no ``WBStartup`` and no ``Rexxc``, because it is meant to land on an existing
  Workbench 3.1 - which the disc also supplies, under ``OS-Version3.1``.

* The **3.2 disc** is copied in the order its installer asks for the floppies,
  and that installer overwrites as it goes - so the Kickstart modules, which
  come last, replace the libraries the Workbench disk brought.

So a release is a list of layers copied newest-last, exactly as the floppy roles
in ``amigaos.py`` are ordered so that Workbench beats Extras.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from . import amigafs, amigainfo, amigaos, iso9660, kickstart, machines
from .machines import Cpu
from .util import LZW_MAGIC, Progress, unlzw

#  AmigaOS 3.5 and 3.9 both refuse to run on anything below a 68020, and both
#  want a Kickstart 3.1 (V40) underneath.  The Kickstart is the requirement that
#  can actually bite on a PiStorm, because Emu68 always provides a 68040-class
#  core but the ROM mapped from the boot partition is whatever was chosen.
NEEDS_CPU = Cpu.M68020
NEEDS_KICKSTART = 40

#  What a floppy image is called on a disc, and the suffix its compressed
#  files carry.
FLOPPY_SUFFIX = ".adf"
COMPRESSED_SUFFIX = ".Z"
#  Stands in a layer's source for the model of the machine being built for.
MODEL = "{model}"


@dataclasses.dataclass(frozen=True)
class Layer:
    """One tree copied off the disc, and where it lands on the system drive.

    ``source`` is a path inside the ISO.  Where it names a floppy image the
    tree is read out of the image, and ``within`` says which drawer - or which
    single file - inside it.  It may hold ``*`` to take every disk of a kind,
    and ``{model}`` for the one that belongs to the machine.
    """

    source: str
    destination: str            # "" is the root of the drive
    label: str
    order: int
    required: bool = False
    within: str = ""
    #  Patterns for the names at the top of the tree: what to take, and what
    #  to leave.  Nothing in ``only`` means everything.
    only: tuple[str, ...] = ()
    leave_out: tuple[str, ...] = ()
    #  A single file copied under another name.
    rename: str = ""
    #  Expand compressed files on the way, dropping the suffix, as the
    #  Installer's own "compression" copy does.
    unpack: bool = False
    #  Copied only when this option is chosen; or only when it is not.
    option: str = ""
    unless: str = ""
    #  Copied only when the Kickstart is older than this.
    below_kickstart: int = 0
    #  Copied only for a processor that is a chip on a board, and at least
    #  this one.  Never for Emu68, which is not one.
    real_cpu: Cpu | None = None
    #  A per-model source whose files are also staged for the other models
    #  the disc ships them byte for byte the same for.  AmigaOS 3.2 keeps the
    #  A600's and the A1200's scsi.device in a drawer named for the model, and
    #  LoadModule picks the drawer by the machine it finds itself on - so a
    #  card written for one and put in the other would otherwise boot with
    #  the ROM's own driver and lose every drive past 4 GB.
    alike_models: bool = False

    def wanted(self, options: frozenset[str],
               kickstart_version: int | None,
               real_cpu: Cpu | None = None) -> bool:
        if self.option and self.option not in options:
            return False
        if self.unless and self.unless in options:
            return False
        if self.real_cpu is not None and not (
                real_cpu is not None and real_cpu.at_least(self.real_cpu)):
            return False
        #  A Kickstart nobody has chosen yet is treated as the older one: what
        #  this guards is copied for the benefit of an old ROM and ignored by a
        #  new one, so copying is the answer that cannot leave a card unable
        #  to boot.
        return not (self.below_kickstart and kickstart_version is not None
                    and kickstart_version >= self.below_kickstart)


@dataclasses.dataclass(frozen=True)
class Option:
    """Something a disc's installer asks, that changes what is copied."""

    key: str
    label: str
    description: str
    default: bool = False


@dataclasses.dataclass(frozen=True)
class Place:
    """Where an icon sits, and how its drawer's window opens.

    What the 3.2 installer does with ``IconPos`` once everything is copied.
    Anything left as None is left as the icon has it.
    """

    path: str                   # the thing the icon belongs to, not the icon
    x: int | None = None
    y: int | None = None
    left: int | None = None
    top: int | None = None
    width: int | None = None
    height: int | None = None
    unless: str = ""


@dataclasses.dataclass(frozen=True)
class Release:
    key: str
    label: str
    #  The volume name the disc carries, used to tell one disc from the other.
    volume: str
    layers: tuple[Layer, ...]
    needs_cpu: Cpu = NEEDS_CPU
    #  The Kickstarts it runs on, oldest and newest; no newest means any.
    kickstart_from: int = NEEDS_KICKSTART
    kickstart_to: int | None = NEEDS_KICKSTART
    options: tuple[Option, ...] = ()
    #  Drawers its installer makes that no layer fills.
    drawers: tuple[str, ...] = ()
    places: tuple[Place, ...] = ()
    #  Where the disc keeps Kickstart ROMs of its own, if it has any.
    roms: str = ""

    @property
    def needs_kickstart(self) -> int:
        return self.kickstart_from

    def chosen(self, options) -> frozenset[str]:
        """The options in force: what was asked for, of what this disc has.

        None means nobody was asked, so the disc's own defaults stand.
        """
        if options is None:
            return frozenset(o.key for o in self.options if o.default)
        return frozenset(options) & {o.key for o in self.options}


#  ---------------------------------------------------------------- 3.5 disc
#
#  "Workbench" here is the 3.5 delta, so the 3.1 trees the disc also carries
#  have to be laid down first or the result has no Startup-Sequence at all.
OS35_LAYERS = (
    Layer("OS-Version3.1/Workbench3.1", "", "Workbench 3.1", 10, required=True),
    Layer("OS-Version3.1/Extras3.1", "", "Extras 3.1", 20),
    Layer("OS-Version3.5/Workbench", "", "Workbench 3.5", 30, required=True),
    Layer("OS-Version3.5/Locale", "Locale", "Locale", 40),
    Layer("OS-Version3.5/Keymaps", "Devs/Keymaps", "Keymaps", 50),
    Layer("OS-Version3.5/Printers", "Devs/Printers", "Printer drivers", 55),
    Layer("OS-Version3.5/C", "C", "Updated commands", 60),
    Layer("OS-Version3.5/L", "L", "FastFileSystem", 65),
    Layer("OS-Version3.5/Extras/Libs", "Libs", "Extra libraries", 70),
    Layer("OS-Version3.5/Extras/Backdrops", "Prefs/Presets/Backdrops",
          "Backdrops", 75),
)

#  ---------------------------------------------------------------- 3.9 disc
#
#  Workbench3.5 is complete on this disc, so no 3.1 layer is needed.  Printers
#  and keymaps come out of the 3.9 tree's own Storage drawer, which is where
#  its installer takes them from.
OS39_LAYERS = (
    Layer("OS-Version3.9/Workbench3.5", "", "Workbench 3.5 base", 10,
          required=True),
    Layer("OS-Version3.9/Workbench3.9", "", "Workbench 3.9", 20, required=True),
    Layer("OS-Version3.9/Locale", "Locale", "Locale", 30),
    Layer("OS-Version3.9/Workbench3.9/Storage/Keymaps", "Devs/Keymaps",
          "Keymaps", 40),
    Layer("OS-Version3.9/Workbench3.9/Storage/Printers", "Devs/Printers",
          "Printer drivers", 45),
    Layer("OS-Version3.9/C", "C", "Updated commands", 50),
    Layer("OS-Version3.9/L", "L", "FastFileSystem", 55),
    Layer("OS-Version3.9/Extras/Libs", "Libs", "Extra libraries", 60),
    Layer("OS-Version3.9/Extras/Backdrops", "Prefs/Presets/Backdrops",
          "Backdrops", 65),
)

#  ---------------------------------------------------------------- 3.2 disc
#
#  Floppy images, in the order Install3.2:Install/Install asks for them.  That
#  script overwrites as it copies, so a later layer replacing an earlier one's
#  file is what it does too.
#
#  What it asks and this does not: which languages, printers and keymaps.  All
#  of them are installed, as they are for 3.5 and 3.9 - choosing is then a
#  matter for Prefs on the Amiga, not for a reinstall.
#
#  The MMULibs disk is for a real 68030, 68040 or 68060 and for nothing else.
#  The boot script runs "CPU CHECKINSTALL", which stops and waits for a key
#  when such a processor has no library of its own - so an accelerator card
#  gets the disk's, as the installer tells its owner to.  Emu68 does not: it
#  carries a 68040.library in its kernel, which is the one that check finds,
#  and the disk's are built on an MMU that Emu68 has not got.
GLOWICONS = Option(
    "glowicons", "Install GlowIcons",
    "The colour icon set from the disc's GlowIcons disk, in place of the "
    "four-colour icons. The disc's own installer asks the same question.",
    default=True)


def _floppy(name: str) -> str:
    return f"ADF/{name}{FLOPPY_SUFFIX}"


_INSTALL = _floppy("Install3.2")
_WORKBENCH = _floppy("Workbench3.2")
_DOCTOR = _floppy("DiskDoctor")
_LOCALE = _floppy("Locale")
_LANGUAGES = _floppy("Locale-*")
_STORAGE = _floppy("Storage3.2")
_MODULES = _floppy(f"Modules{MODEL}_3.2")
_MMULIBS = _floppy("MMULibs")
_ICON = ("Disk.info",)

OS32_LAYERS = (
    Layer(_INSTALL, "Tools", "Hard disk tools", 10, within="HDTools",
          only=("hd*", "bru*")),
    Layer(_INSTALL, "System", "Installer", 11, within="Installer"),
    Layer(_INSTALL, "Libs", "workbench.library", 12, required=True,
          within="Libs/workbench.library"),
    Layer(_INSTALL, "Libs", "icon.library", 13, required=True,
          within="Libs/icon.library"),
    Layer(_WORKBENCH, "", "Workbench 3.2", 20, required=True,
          leave_out=("Locale",) + _ICON),
    Layer(_DOCTOR, "C", "DAControl", 22, within="C/DAControl"),
    Layer(_DOCTOR, "C", "DiskDoctor", 23, within="C/DiskDoctor"),
    Layer(_DOCTOR, "Devs", "trackfile.device", 24,
          within="Devs/trackfile.device"),
    Layer(_LOCALE, "Locale/Countries", "Countries", 30, within="Countries"),
    Layer(_LOCALE, "Fonts", "Fonts for other alphabets", 31,
          within="Support/Fonts", unpack=True),
    Layer(_LANGUAGES, "Locale/Languages", "Languages", 32,
          within="Languages"),
    Layer(_LANGUAGES, "Locale/Catalogs", "Catalogs", 33, within="Catalogs",
          unpack=True),
    Layer(_LANGUAGES, "Locale/Help", "Help", 34, within="Help", unpack=True),
    Layer(_LANGUAGES, "", "Language support", 35, within="Support",
          leave_out=("Fonts",)),
    Layer(_LANGUAGES, "Fonts", "Language fonts", 36, within="Support/Fonts",
          unpack=True),
    Layer(_floppy("Extras3.2"), "", "Extras 3.2", 45, required=True,
          leave_out=_ICON),
    Layer(_floppy("Classes3.2"), "", "Classes", 50, leave_out=_ICON),
    Layer(_floppy("Fonts"), "Fonts", "Fonts", 55, leave_out=_ICON),
    Layer(_STORAGE, "Storage", "Storage drawer icons", 60,
          only=("DataTypes.info", "DOSDrivers.info", "KeyMaps.info",
                "Monitors.info", "Printers.info")),
    Layer(_STORAGE, "Classes/DataTypes", "DataTypes", 61,
          within="Classes/DataTypes"),
    Layer(_STORAGE, "C", "More commands", 62, within="C"),
    Layer(_STORAGE, "Prefs/Env-Archive/Sys", "Default icons", 63,
          within="DefIcons", only=("*.info",)),
    Layer(_STORAGE, "Prefs/Presets/Pointers", "Pointers", 64,
          within="Presets/Pointers"),
    Layer(_STORAGE, "Storage/Monitors", "Monitors", 65, within="Monitors"),
    Layer(_STORAGE, "Storage/DOSDrivers", "DOS drivers", 66,
          within="DOSDrivers"),
    Layer(_STORAGE, "WBStartup", "WBStartup", 67, within="WBStartup"),
    Layer(_STORAGE, "Prefs/Env-Archive", "DefIcons settings", 68,
          within="Env-Archive/deficons.prefs"),
    Layer(_STORAGE, "Prefs/Env-Archive/Sys", "Pointer settings", 69,
          within="Env-Archive/Pointer.prefs"),
    Layer(_STORAGE, "Devs/Printers", "Printer drivers", 70,
          within="Printers"),
    Layer(_STORAGE, "Devs/Keymaps", "Keymaps", 71, within="Keymaps"),
    Layer(_STORAGE, "Libs", "More libraries", 72, within="LIBS"),
    Layer(_floppy("Backdrops3.2"), "Prefs/Presets/Backdrops", "Backdrops", 75,
          leave_out=_ICON),
    #  The installer deletes Trashcan.info again where there is no Trashcan,
    #  and on a drive it has just made there is none.
    Layer(_floppy("GlowIcons3.2"), "", "GlowIcons", 80,
          leave_out=("Trashcan.info",), option=GLOWICONS.key),
    #  The ROM's own modules, for a Kickstart older than 3.2: the boot script
    #  below loads them over a 3.1 ROM with LoadModule.
    Layer(_MODULES, "Devs", "Kickstart modules (Devs)", 85, within="DEVS",
          below_kickstart=47, alike_models=True),
    Layer(_MODULES, "L", "Kickstart modules (L)", 86, within="L",
          below_kickstart=47),
    Layer(_MODULES, "Libs", "Kickstart modules (Libs)", 87, within="LIBS",
          below_kickstart=47),
    Layer(_MMULIBS, "", "Processor libraries", 88,
          leave_out=("Configs", "Locale"), real_cpu=Cpu.M68030),
    Layer(_MMULIBS, "MuTools", "Processor library guides", 89,
          within="Locale/Help/MMULib", unpack=True, real_cpu=Cpu.M68030),
    #  The installer asks who made the board, to pick a configuration written
    #  for it.  This is the one it uses when the answer is nobody it knows.
    Layer(_MMULIBS, "Prefs/Env-Archive", "MMU configuration", 89,
          within="Configs/MMU-Configuration", real_cpu=Cpu.M68030),
    Layer(_INSTALL, "", "Disk icon", 90, within="Update/Disk.info",
          unless=GLOWICONS.key),
    Layer(_INSTALL, "S", "Startup-Sequence", 91, required=True,
          within="Update/Startup-HardDrive", rename="Startup-Sequence"),
    Layer(_INSTALL, "Prefs/Env-Archive/Versions", "Release", 92,
          within="Update/Release"),
    Layer(_WORKBENCH, "", "Storage icon", 93, within="Devs.info",
          rename="Storage.info", unless=GLOWICONS.key),
)

OS32_DRAWERS = (
    "Prefs/Env-Archive/Sys", "Prefs/Env-Archive/Versions",
    "Prefs/Presets/Backdrops", "Prefs/Presets/Pointers", "Fonts", "Expansion",
    "WBStartup", "Locale/Catalogs", "Locale/Languages", "Locale/Countries",
    "Locale/Help", "Classes/Gadgets", "Classes/DataTypes", "Classes/Images",
    "Devs/Monitors", "Devs/DataTypes", "Devs/DOSDrivers", "Devs/Printers",
    "Devs/Keymaps", "Storage/DOSDrivers", "Storage/Printers",
    "Storage/Monitors", "Storage/Keymaps", "Storage/DataTypes", "Libs",
    "Tools", "System",
)

#  Only for the four-colour icons; the GlowIcons disk brings its own places.
OS32_PLACES = tuple(
    dataclasses.replace(place, unless=GLOWICONS.key) for place in (
        Place("Prefs", 12, 20),
        Place("Prefs/Printer", 160, 48),
        Place("Utilities", 98, 4),
        Place("Utilities/Clock", 91, 11),
        Place("Utilities/MultiView", 11, 11),
        Place("Tools", 98, 38),
        Place("Tools/IconEdit", 111, 45),
        Place("Tools/HDToolBox", 202, 4),
        Place("System", 184, 4, height=150),
        Place("WBStartup", 184, 38),
        Place("Devs", 270, 4),
        Place("Storage", 270, 38, left=480, top=77, width=110, height=199),
        Place("Storage/Monitors", left=156, top=77, width=270, height=199),
        Place("Storage/Printers", left=480, top=77, width=107, height=199),
        Place("Expansion", 356, 20),
        Place("Disk", left=28, top=39, width=462, height=103),
    ))

RELEASES = (
    #  3.2 runs on a 68000, and on any Kickstart from 3.1 up: on a 3.2 ROM as
    #  it is, and on an older one by loading the modules above.
    Release("3.2", "AmigaOS 3.2", "AmigaOS3.2CD", OS32_LAYERS,
            needs_cpu=Cpu.M68000, kickstart_to=None, options=(GLOWICONS,),
            drawers=OS32_DRAWERS, places=OS32_PLACES, roms="ROM"),
    Release("3.5", "AmigaOS 3.5", "AmigaOS3.5", OS35_LAYERS),
    Release("3.9", "AmigaOS 3.9", "AmigaOS3.9", OS39_LAYERS),
)

RELEASES_BY_KEY = {r.key: r for r in RELEASES}


def release_names(joiner: str = "or") -> str:
    """The releases a CD can be, as a sentence would list them."""
    keys = [release.key for release in RELEASES]
    return ", ".join(keys[:-1]) + f" {joiner} {keys[-1]}"


def kickstart_wanted(release: Release) -> str:
    """The Kickstart a release asks for, in words."""
    if release.kickstart_to is None:
        return f"Kickstart 3.1 (V{release.kickstart_from}) or newer"
    return f"Kickstart 3.1 (V{release.kickstart_from})"


#  ------------------------------------------------------- reading a layer


@dataclasses.dataclass(frozen=True)
class _Item:
    """One file or drawer of a layer, wherever on the disc it lives."""

    relative: str               # below the layer's destination
    is_dir: bool
    size: int
    save: Callable[[Path], None] | None = None
    known: amigaos.Metadata | None = None


class _Disc:
    """An open disc, and the floppy images on it that have been opened."""

    def __init__(self, iso: iso9660.IsoImage):
        self.iso = iso
        self._floppies: dict[int, amigafs.Volume] = {}

    def sources(self, layer: Layer, model: str = "") -> list[iso9660.Entry]:
        """Every entry on the disc a layer's source names.

        With no model given, a source that depends on one is answered for
        whichever model comes first: enough to say whether the disc has such
        disks at all, which is all that can be asked before a machine is.
        """
        folder, _, name = layer.source.rpartition("/")
        pattern = name.replace(MODEL, model or "*").lower()
        if "*" not in pattern:
            found = self.iso.find(layer.source.replace(MODEL, model))
            return [found] if found is not None else []
        parent = self.iso.find(folder)
        if parent is None or not parent.is_dir:
            return []
        matches = sorted((child for child in self.iso.listdir(parent)
                          if fnmatch.fnmatchcase(child.name.lower(), pattern)),
                         key=lambda child: child.name.lower())
        return matches if model or MODEL not in name else matches[:1]

    def floppy(self, entry: iso9660.Entry) -> amigafs.Volume:
        volume = self._floppies.get(entry.lba)
        if volume is None:
            volume = amigafs.Volume(self.iso.handle, entry.offset,
                                    entry.size // amigafs.BLOCK)
            self._floppies[entry.lba] = volume
        return volume

    def holds(self, layer: Layer, entry: iso9660.Entry) -> bool:
        """Whether this source really has the tree the layer is after."""
        if not _is_floppy(entry):
            return entry.is_dir
        try:
            volume = self.floppy(entry)
            return not layer.within or volume.find(layer.within) is not None
        except (amigafs.AmigaFsError, OSError, ValueError):
            return False

    def items(self, layer: Layer, entry: iso9660.Entry) -> Iterator[_Item]:
        found = self._floppy_items(layer, entry) if _is_floppy(entry) \
            else self._tree_items(entry)
        for item in found:
            top = item.relative.partition("/")[0].lower()
            if layer.only and not _matches(top, layer.only):
                continue
            if _matches(top, layer.leave_out):
                continue
            yield item if not layer.unpack else _unpacked(item)

    def _tree_items(self, entry: iso9660.Entry) -> Iterator[_Item]:
        for relative, child in self.iso.walk(entry):
            yield _Item(relative, child.is_dir, child.size,
                        None if child.is_dir else
                        lambda into, child=child: self.iso.extract(child, into))

    def _floppy_items(self, layer: Layer,
                      entry: iso9660.Entry) -> Iterator[_Item]:
        volume = self.floppy(entry)
        root = volume.find(layer.within) if layer.within else None
        if layer.within and root is None:
            return
        if root is not None and root.is_file:
            yield _floppy_item(volume, layer.rename or root.name, root)
            return
        for relative, child in volume.walk(root.block if root else None):
            yield _floppy_item(volume, relative, child)


def _is_floppy(entry: iso9660.Entry) -> bool:
    return not entry.is_dir and entry.name.lower().endswith(FLOPPY_SUFFIX)


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern.lower())
               for pattern in patterns)


def _floppy_item(volume: amigafs.Volume, relative: str,
                 entry: amigafs.Entry) -> _Item:
    known = amigaos.Metadata(entry.protect & 0xFF, entry.days, entry.mins,
                             entry.ticks, entry.comment)
    if entry.is_dir:
        return _Item(relative, True, 0, known=known)
    return _Item(relative, False, entry.size,
                 lambda into: _save(into, volume.read_file(entry)), known)


def _save(into: Path, data: bytes) -> None:
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_bytes(data)


def _unpacked(item: _Item) -> _Item:
    """The same file as it will be once expanded, under the name it has then."""
    if item.is_dir or not item.relative.endswith(COMPRESSED_SUFFIX):
        return item

    def save(into: Path) -> None:
        item.save(into)
        data = into.read_bytes()
        #  By what it holds and not only by its name: a file that merely ends
        #  in .Z is copied as it is rather than refused.
        if data[:2] == LZW_MAGIC:
            into.write_bytes(unlzw(data))

    return dataclasses.replace(
        item, relative=item.relative[:-len(COMPRESSED_SUFFIX)], save=save)


class _Staging:
    """The tree being laid out, spelled as an Amiga would find it.

    AmigaDOS does not tell ``LIBS`` from ``Libs`` and Linux does, and the disks
    of one release do not agree on which to write.  Left alone that makes two
    drawers where the Amiga has one, and a later layer's file lands beside the
    one it was meant to replace instead of on top of it.
    """

    def __init__(self, root: Path):
        self.root = root
        self._names: dict[Path, dict[str, str]] = {}

    def _known(self, folder: Path) -> dict[str, str]:
        known = self._names.get(folder)
        if known is None:
            known = {name.lower(): name for name in os.listdir(folder)} \
                if folder.is_dir() else {}
            self._names[folder] = known
        return known

    def drawer(self, relative: str) -> Path:
        folder = self.root
        for part in (p for p in relative.split("/") if p):
            folder = folder / self._known(folder).setdefault(part.lower(), part)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def find(self, relative: str) -> Path | None:
        """What is already there under this name, however it is spelled."""
        folder = self.root
        for part in (p for p in relative.split("/") if p):
            actual = self._known(folder).get(part.lower())
            if actual is None:
                return None
            folder = folder / actual
        return folder if folder.exists() else None

    def file(self, relative: str) -> Path:
        """Where a file goes, with whatever had its name cleared away."""
        parent, _, name = relative.rpartition("/")
        folder = self.drawer(parent)
        known = self._known(folder)
        for stale in {known.get(name.lower(), name), name}:
            amigaos.sidecar_of(folder / stale).unlink(missing_ok=True)
            if stale != name:
                (folder / stale).unlink(missing_ok=True)
        known[name.lower()] = name
        return folder / name


@dataclasses.dataclass
class CdMatch:
    """What was found on a disc image handed to us."""

    path: Path
    volume_name: str = ""
    release: Release | None = None
    #  Only the layers actually present, so a disc that is missing a tree says
    #  so rather than failing part-way through a build.
    present: tuple[Layer, ...] = ()
    missing: tuple[Layer, ...] = ()
    files: int = 0
    total_bytes: int = 0
    error: str = ""

    @property
    def usable(self) -> bool:
        return (self.release is not None and not self.error
                and not any(layer.required for layer in self.missing))

    @property
    def label(self) -> str:
        if self.error:
            return f"{self.path.name}: {self.error}"
        if self.release is None:
            return f'{self.path.name}: "{self.volume_name}" is not an AmigaOS CD'
        note = "" if self.usable else " - incomplete"
        return (f'"{self.volume_name}" -> {self.release.label}{note} '
                f"({self.files} files)")


def identify(path: str | Path) -> CdMatch:
    """Work out which AmigaOS release a CD image holds, by looking inside it.

    By the volume name *and* the trees, not by the file name: the discs in
    circulation are named every possible way, and a name is not evidence.
    """
    path = Path(path)
    try:
        with iso9660.IsoImage.open(path) as iso:
            disc = _Disc(iso)
            volume = iso.volume_name
            release = next((r for r in RELEASES
                            if r.volume.lower() == volume.strip().lower()), None)
            if release is None:
                #  The volume name is the quick answer; if it is not one we
                #  know, fall back to looking for the trees themselves, because
                #  a re-mastered disc keeps the layout and loses the label.
                release = _release_by_layout(disc)
            if release is None:
                return CdMatch(path, volume_name=volume)

            #  What the disc's own defaults would install, so the figures are
            #  the ones a build with nothing changed would reach.
            chosen = release.chosen(None)
            present, missing = [], []
            files = total = 0
            for layer in release.layers:
                sources = [entry for entry in disc.sources(layer)
                           if disc.holds(layer, entry)]
                if not sources:
                    missing.append(layer)
                    continue
                present.append(layer)
                if not layer.wanted(chosen, None):
                    continue
                for entry in sources:
                    for item in disc.items(layer, entry):
                        if not item.is_dir:
                            files += 1
                            total += item.size
            return CdMatch(path, volume_name=volume, release=release,
                           present=tuple(present), missing=tuple(missing),
                           files=files, total_bytes=total)
    except (iso9660.Iso9660Error, amigafs.AmigaFsError, OSError,
            ValueError) as error:
        return CdMatch(path, error=str(error))


def _release_by_layout(disc: _Disc) -> Release | None:
    """Recognise a disc whose volume name has been changed, by its trees."""
    for release in RELEASES:
        required = [layer for layer in release.layers if layer.required]
        if required and all(any(disc.holds(layer, entry)
                                for entry in disc.sources(layer))
                            for layer in required):
            return release
    return None


def stage(match: CdMatch, into: str | Path,
          progress: Progress | None = None, *,
          machine: machines.Machine | None = None,
          kickstart_version: int | None = None,
          real_cpu: Cpu | None = None,
          options=None) -> int:
    """Lay the disc's trees out on disk, in order, as the drive should look.

    The layering is resolved here rather than on the Amiga volume, and that is
    not an implementation detail: the volume writer in this project creates
    files and never overwrites them, so whatever lands first wins.  Copying the
    3.5 tree and then the 3.9 tree straight onto it would keep the *3.5* file
    every time the two discs carry the same name - which is every important
    file on the disc, and exactly backwards.

    On a plain Linux directory a later copy replaces an earlier one, so the
    order below produces the tree the Amiga should end up with, and the volume
    is then written once from it.

    ``machine``, ``kickstart_version`` and ``real_cpu`` - the processor, where
    it is a chip rather than Emu68 - decide the layers that depend on them,
    and ``options`` the ones that were a question; with none given the disc's
    own defaults stand.
    """
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    release = match.release
    if release is None:
        raise RuntimeError(f"{match.path.name} is not an AmigaOS CD")
    chosen = release.chosen(options)
    model = machine.amiga_model if machine is not None else ""
    staging = _Staging(into)
    written = 0
    with iso9660.IsoImage.open(match.path) as iso:
        disc = _Disc(iso)
        for layer in sorted(match.present, key=lambda item: item.order):
            if not layer.wanted(chosen, kickstart_version, real_cpu):
                continue
            sources = [entry for entry in disc.sources(layer, model)
                       if disc.holds(layer, entry)]
            if not sources and layer.required:
                raise RuntimeError(
                    f"{match.path.name} has no {layer.label} for "
                    f"{machine.label if machine else 'this machine'}.")
            count = 0
            for entry in sources:
                for item in disc.items(layer, entry):
                    relative = f"{layer.destination}/{item.relative}"
                    if item.is_dir:
                        staging.drawer(relative)
                        continue
                    target = staging.file(relative)
                    item.save(target)
                    if item.known is not None:
                        amigaos.write_sidecar(target,
                                              **item.known.as_arguments())
                    count += 1
            if layer.alike_models and model:
                count += _stage_alike(disc, layer, model, staging, progress)
            written += count
            if progress is not None:
                progress.log(f"  {layer.label}: {count} files -> "
                             f"{layer.destination or 'the root of the drive'}")
    for drawer in release.drawers:
        staging.drawer(drawer)
    for place in release.places:
        if place.unless and place.unless in chosen:
            continue
        icon = staging.find(place.path + amigaos.ICON_SUFFIX)
        if icon is not None:
            icon.write_bytes(amigainfo.place(
                icon.read_bytes(), x=place.x, y=place.y, left=place.left,
                top=place.top, width=place.width, height=place.height))
    return written


def _model_in(layer: Layer, entry: iso9660.Entry) -> str:
    """The model a per-model source was found for, read from its name."""
    before, _, after = layer.source.rpartition("/")[2].partition(MODEL)
    name = entry.name
    if not (name.lower().startswith(before.lower())
            and name.lower().endswith(after.lower())):
        return ""
    return name[len(before):len(name) - len(after)]


def _stage_alike(disc: _Disc, layer: Layer, model: str, staging: _Staging,
                 progress: Progress | None) -> int:
    """Stage another model's copy of each file it shares with ``model``.

    Only a file kept in a drawer named for its model, and only where the
    other model's disk has exactly the bytes already staged for this one:
    the same driver under two names, never a different driver guessed at.
    """
    staged = 0
    for entry in disc.sources(layer, "*"):
        other = _model_in(layer, entry)
        if not other or other.lower() == model.lower() \
                or not disc.holds(layer, entry):
            continue
        for item in disc.items(layer, entry):
            parts = item.relative.split("/")
            if item.is_dir or len(parts) < 2 \
                    or parts[0].lower() != other.lower():
                continue
            ours = staging.find(f"{layer.destination}/{model}/"
                                + "/".join(parts[1:]))
            if ours is None or not ours.is_file():
                continue
            relative = f"{layer.destination}/{item.relative}"
            with tempfile.TemporaryDirectory() as scratch:
                theirs = Path(scratch) / "file"
                item.save(theirs)
                if theirs.read_bytes() != ours.read_bytes():
                    continue
                target = staging.file(relative)
                shutil.copyfile(theirs, target)
            if item.known is not None:
                amigaos.write_sidecar(target, **item.known.as_arguments())
            staged += 1
            if progress is not None:
                progress.log(f"    {relative}: the same file as the "
                             f"{model}'s, so a card moved to an {other} "
                             f"loads it too")
    return staged


def kickstart_on_disc(match: CdMatch, machine: machines.Machine,
                      into: str | Path) -> kickstart.RomInfo | None:
    """The Kickstart the disc carries for this machine, lifted out of it.

    Only of use where the Kickstart is a file - Emu68 maps one from the boot
    partition.  A machine running from the ROM chip on its board has whatever
    is soldered to it, and nothing on a disc changes that.

    Found by the model in its name and proved by reading it: the disc keeps
    other things beside its Kickstarts, and one ROM serves several models.
    """
    if match.release is None or not match.release.roms:
        return None
    wanted = machine.amiga_model.lower()
    try:
        with iso9660.IsoImage.open(match.path) as iso:
            folder = iso.find(match.release.roms)
            if folder is None or not folder.is_dir:
                return None
            for entry in sorted(iso.listdir(folder),
                                key=lambda item: item.name.lower()):
                name = entry.name.lower()
                if entry.is_dir or not name.endswith(".rom") \
                        or wanted not in name \
                        or entry.size not in kickstart.VALID_SIZES:
                    continue
                #  Written afresh each time.  A copy already there is only
                #  the same ROM if it came off the same disc, and its name
                #  and size cannot say so.
                target = Path(into) / f"{match.volume_name}-{entry.name}"
                iso.extract(entry, target)
                found = kickstart.identify(target, "")
                if found.usable and found.version is not None \
                        and found.version >= match.release.kickstart_from:
                    return found
    except (iso9660.Iso9660Error, OSError, ValueError):
        return None
    return None


def requirements(release: Release, machine: machines.Machine,
                 accelerator: machines.Accelerator,
                 card_cpu: Cpu | None = None,
                 kickstart_version: int | None = None) -> list[str]:
    """What stops this release running on this machine, in plain words.

    An empty list means nothing does.  Both checks are made every time even
    though one of them cannot currently fail on a PiStorm: the processor
    requirement belongs to AmigaOS rather than to today's accelerator, and
    stating it here is what makes the machine model answer the question rather
    than a comment somewhere describing why it was not asked.
    """
    problems: list[str] = []

    cpu = machine.cpu_fitted(accelerator, card_cpu)
    if not cpu.at_least(release.needs_cpu):
        problems.append(
            f"{release.label} needs a {release.needs_cpu.label} or better, and "
            f"{machine.label} with a {accelerator.label.lower()} has a "
            f"{cpu.label}.")

    #  A Kickstart is only wrong once one has been chosen; not having chosen
    #  yet is a different complaint and belongs to whatever asks for one.
    if kickstart_version is not None and not (
            release.kickstart_from <= kickstart_version
            and (release.kickstart_to is None
                 or kickstart_version <= release.kickstart_to)):
        problems.append(
            f"{release.label} needs {kickstart_wanted(release)}, and "
            f"the chosen ROM is V{kickstart_version}.")

    return problems
