"""A games menu for ArcadeGameSelector 2, built from the games on the card.

AGS2 does not look for games. It shows a drawer tree: a drawer called
``Name.ags`` is a submenu, a file ``Name.run`` is an entry - an AmigaDOS
script it executes when the entry is chosen - and ``Name.txt`` and
``Name.iff`` beside it are the entry's text and picture. So the menu is
written here, at build time, from the drives the card is being filled with,
in the shape the collection already has: a drive organised as
``WHDLoad/AGA/A/Agony`` becomes ``AGA > A > Agony``.

Nothing is invented. The name and the text of each entry are the slave's
own, read from its header; the WHDLoad arguments are the game's own icon
settings, because WHDLoad reads tool types only when started from Workbench
and AGS2 starts it from a script; and only the options WHDLoad itself names
in its binary are passed on, so an icon's NewIcons image data, or a tool type
some other program left there, cannot break the launch.
"""
from __future__ import annotations

import dataclasses
import os
import re
import struct
import textwrap
from pathlib import Path

from . import amigainfo
from .util import Progress

SLAVE_ID = b"WHDLOADS"
#  The slave header's own layout (WHDLoad's include/whdload.i): ws_Security
#  is the first long, ws_ID the eight bytes after it, then ws_Version; from
#  version 10, ws_name, ws_copy and ws_info are word offsets from the header.
NAME_FIELDS = (36, 38, 40)
#  AGS2 keeps a name in 64 bytes and shows 26 of them.
NAME_LIMIT = 60
#  Its own 2022 build follows eight levels of submenu.
DEPTH_LIMIT = 7
IFF_EXTENSIONS = (".iff", ".ilbm")


@dataclasses.dataclass(frozen=True)
class Layout:
    """Where AGS2 draws the menu, the text and the picture, and what fits."""

    name: str
    conf: str
    #  The largest picture the layout has room for: (width, height, planes).
    picture: tuple[int, int, int]
    text_width: int
    text_height: int
    #  Pictures this build draws itself, as (file, width, height, planes);
    #  empty where the layout's own come from the AGS2 release.
    makes: tuple[tuple[str, int, int, int], ...] = ()


#  AGS2's own 16-colour layout, from its release: every chipset can show it.
#  Its picture box is 320x128 in 16 colours, which a WHDLoad screenshot - a
#  full 320x256 screen, usually in 256 colours - does not fit.
NATIVE = Layout(
    "WB13",
    "# AGS2.conf - written by the PiStorm imager: AGS2's WB13 layout,\n"
    "# 640x256 in 16 colours, which every chipset can show.\n"
    "background = AGS:WB13-Background.iff\n"
    "empty_screenshot = AGS:WB13-Empty.iff\n"
    "menu_x = 32\nmenu_y = 8\nmenu_height = 30\n"
    "screenshot_x = 288\nscreenshot_y = 8\n"
    "text_x = 288\ntext_y = 152\ntext_width = 40\ntext_height = 12\n"
    "blue_button_action = quit\n",
    picture=(320, 128, 4), text_width=40, text_height=12)

#  For AGA: a screen the screenshots fit whole. The menu and the text share
#  the left half and the picture has the right; the background is plain and
#  drawn here, because the release's AGA artwork is laid out for a picture
#  box 200 lines high and these are 256.
AGA = Layout(
    "AGA",
    "# AGS2.conf - written by the PiStorm imager: 640x256 in 256 colours,\n"
    "# the menu and the text on the left and the game's own screenshot,\n"
    "# 320x256, on the right.\n"
    "background = AGS:Menu-Background.iff\n"
    "empty_screenshot = AGS:Menu-Empty.iff\n"
    "text_color = 1\ntext_background = 0\nlock_colors = 2\n"
    "menu_x = 8\nmenu_y = 8\nmenu_height = 16\n"
    "screenshot_x = 320\nscreenshot_y = 0\n"
    "text_x = 8\ntext_y = 144\ntext_width = 38\ntext_height = 13\n"
    "blue_button_action = quit\n",
    picture=(320, 256, 8), text_width=38, text_height=13,
    makes=(("Menu-Background.iff", 640, 256, 8),
           ("Menu-Empty.iff", 320, 256, 8)))


def slave_header(data: bytes) -> tuple[str, str, str] | None:
    """A slave's own name, copyright and info, or None if it has none."""
    at = data.find(SLAVE_ID)
    if at < 4:
        return None
    base = at - 4
    try:
        version = struct.unpack_from(">H", data, at + 8)[0]
        if version < 10:
            return None
        texts = []
        for field in NAME_FIELDS:
            offset = struct.unpack_from(">H", data, base + field)[0]
            raw = (data[base + offset:base + offset + 512].split(b"\0")[0]
                   if offset else b"")
            #  0xFF is WHDLoad's line break in these strings.
            texts.append(raw.replace(b"\xff", b"\n").decode("latin-1").strip())
    except struct.error:
        return None
    return texts[0], texts[1], texts[2]


def picture_size(data: bytes) -> tuple[int, int, int] | None:
    """An ILBM's width, height and planes, from its BMHD."""
    if data[:4] != b"FORM" or data[8:12] not in (b"ILBM", b"PBM "):
        return None
    at = data.find(b"BMHD", 12, 4096)
    if at < 0:
        return None
    width, height = struct.unpack_from(">HH", data, at + 8)
    return width, height, data[at + 16]


def plain_picture(width: int, height: int, planes: int) -> bytes:
    """A black ILBM with colour 1 white, for the text AGS2 draws on it."""
    def chunk(name: bytes, body: bytes) -> bytes:
        return name + struct.pack(">I", len(body)) + body + (
            b"\0" if len(body) % 2 else b"")
    colours = 1 << planes
    cmap = bytearray(3 * colours)
    cmap[3:6] = b"\xff\xff\xff"
    row = ((width + 15) // 16) * 2
    bmhd = struct.pack(">HHhhBBBBHBBhh", width, height, 0, 0, planes, 0, 0, 0,
                       0, 10, 11, width, height)
    body = (chunk(b"BMHD", bmhd) + chunk(b"CMAP", bytes(cmap))
            + chunk(b"BODY", bytes(row * planes * height)))
    return b"FORM" + struct.pack(">I", 4 + len(body)) + b"ILBM" + body


def option_words(whdload: Path) -> set[str]:
    """WHDLoad's own option names, for telling its tool types apart.

    WHDLoad parses its Shell arguments against its options, and a tool type
    that is not one of them - NewIcons keeps its image in IM1= lines, and
    other programs leave theirs - would make it refuse the whole command
    line. The options are read from the documentation in the same archive
    as the program, whose every option is an anchor; failing that, every
    word in the program itself, which is looser but never drops a real one.
    """
    docs = whdload.parent.parent / "Docs" / "en" / "opt.html"
    try:
        names = re.findall(r'<a name="([A-Za-z][A-Za-z0-9]*)"',
                           docs.read_text(encoding="latin-1"))
        if names:
            return {name.lower() for name in names}
    except OSError:
        pass
    try:
        data = whdload.read_bytes()
    except OSError:
        return set()
    return {word.decode("latin-1").lower()
            for word in re.findall(rb"[A-Za-z][A-Za-z0-9]{1,23}", data)}


def arguments(tooltypes: list[str], slave: str, words: set[str]) -> str:
    """The game's icon settings, as the WHDLoad command line they mean."""
    out = [f'SLAVE="{slave}"']
    for line in tooltypes:
        line = line.strip()
        if not line or line[0] in "(;*":
            continue
        key, equals, value = line.partition("=")
        key = key.strip()
        #  Custom1 to Custom5 are documented together, under Custom.
        known = key.lower() in words or key.lower().rstrip("0123456789") \
            in words
        if key.lower() == "slave" or not known:
            continue
        if not equals:
            out.append(key)
        elif " " in value or not value:
            out.append(f'{key}="{value}"')
        else:
            out.append(f"{key}={value}")
    return " ".join(out)


def _safe_name(name: str, taken: set[str]) -> str:
    """A name AGS2 and the file system can both hold, unique in its drawer."""
    name = re.sub(r"[:/\x00-\x1f]", " ", name).strip() or "Game"
    name = re.sub(r"\s+", " ", name)[:NAME_LIMIT - 5].strip()
    unique, count = name, 2
    while unique.lower() in taken:
        unique = f"{name} ({count})"
        count += 1
    taken.add(unique.lower())
    return unique


@dataclasses.dataclass
class Drive:
    """A drive the card is being filled with, as the menu needs it."""

    volume: str               # its Amiga volume name
    folder: Path              # where its files are on this computer
    leave_out: tuple[str, ...] = ()   # paths in it the build leaves off
    #  The drawer a collection keeps its installs in, which a menu need not
    #  repeat as a level of its own.
    collection: str = ""


def build(drives: list[Drive], into: Path, layout: Layout, words: set[str],
          media: bool, progress: Progress) -> int:
    """Write the menu tree for ``drives`` into ``into``; how many entries.

    ``media`` is whether to bring each game's screenshot - asked, because it
    is a few hundred kilobytes a game and not everybody wants pictures.
    """
    into.mkdir(parents=True, exist_ok=True)
    (into / "AGS2.conf").write_text(layout.conf)
    for name, width, height, planes in layout.makes:
        (into / name).write_bytes(plain_picture(width, height, planes))
    several = len(drives) > 1
    count = 0
    for drive in drives:
        top = into / f"{_safe_name(drive.volume, set())}.ags" if several \
            else into
        count += _drive(drive, top, layout, words, media, progress)
    return count


def _drive(drive: Drive, top: Path, layout: Layout, words: set[str],
           media: bool, progress: Progress) -> int:
    games = _games(drive)
    #  Named once every game in a submenu is known: language versions of a
    #  game share their slave's title, so where titles clash each is told
    #  apart by its own drawer - "Kick Off 2 (KickOff2Fr)" - rather than by
    #  a number that says nothing.
    titles: dict[Path, dict[str, int]] = {}
    for game in games:
        seen = titles.setdefault(game.menu, {})
        seen[game.title.lower()] = seen.get(game.title.lower(), 0) + 1
    names: dict[Path, set[str]] = {}
    skipped = 0
    for game in games:
        clash = titles[game.menu][game.title.lower()] > 1
        wanted = f"{game.title} ({game.drawer.name})" if clash else game.title
        name = _safe_name(wanted, names.setdefault(game.menu, set()))
        menu = top / game.menu
        menu.mkdir(parents=True, exist_ok=True)
        if game.tooltypes is None:
            skipped += 1
        amiga = f"{drive.volume}:{game.relative}" if game.relative != "." \
            else f"{drive.volume}:"
        script = (f"; Written by the PiStorm imager from {amiga}\n"
                  f'Cd "{amiga}"\n'
                  f"WHDLoad {arguments(game.tooltypes or [], game.slave, words)}"
                  f"\n")
        (menu / f"{name}.run").write_text(script, encoding="latin-1",
                                          errors="replace")
        if media:
            _media(game.drawer, menu / name, game.header, layout)
    if skipped:
        progress.log(f"  {skipped} game(s) on {drive.volume} have no icon to "
                     f"take settings from; they start with WHDLoad's own "
                     f"defaults")
    return len(games)


@dataclasses.dataclass
class _Game:
    menu: Path               # the submenu, relative to the drive's top
    drawer: Path             # the game's drawer on this computer
    relative: str            # the same, as a path on the drive
    slave: str
    header: tuple[str, str, str] | None
    tooltypes: list[str] | None

    @property
    def title(self) -> str:
        return (self.header[0] if self.header and self.header[0]
                else self.drawer.name)


def _games(drive: Drive) -> list[_Game]:
    """Every game drawer on the drive - one holding a slave - in order."""
    out: list[_Game] = []
    leave = tuple(p.strip("/").lower() for p in drive.leave_out)
    for dirpath, dirnames, filenames in os.walk(drive.folder):
        dirnames.sort(key=str.lower)
        here = Path(dirpath)
        relative = here.relative_to(drive.folder).as_posix()
        if relative != "." and any(relative.lower() == p
                                   or relative.lower().startswith(p + "/")
                                   for p in leave):
            dirnames[:] = []
            continue
        slaves = sorted(f for f in filenames if f.lower().endswith(".slave"))
        if not slaves:
            continue
        #  A game drawer: nothing below it is another game.
        dirnames[:] = []
        parts = list(Path(relative).parts)
        #  The collection's own top drawer says nothing a menu needs to
        #  repeat.
        if len(parts) > 1 and drive.collection \
                and parts[0].lower() == drive.collection.lower():
            parts = parts[1:]
        menu = Path(*[f"{_safe_name(part, set())}.ags"
                      for part in parts[:-1][-DEPTH_LIMIT:]]) \
            if len(parts) > 1 else Path()
        slave = slaves[0]
        try:
            header = slave_header((here / slave).read_bytes())
        except OSError:
            header = None
        out.append(_Game(menu, here, relative, slave, header,
                         _icon_tooltypes(here, slave)))
    return out


def _icon_tooltypes(drawer: Path, slave: str) -> list[str] | None:
    """The tool types of the icon that starts ``slave``, if there is one."""
    found = None
    for icon in sorted(drawer.glob("*.info")):
        try:
            types = amigainfo.read_tooltypes(icon.read_bytes())
        except (amigainfo.InfoError, OSError):
            continue
        named = [t.partition("=")[2].strip() for t in types
                 if t.upper().startswith("SLAVE=")]
        if any(n.lower() == slave.lower() for n in named):
            return types
        if named and found is None:
            found = types
    return found


def _beside(entry: Path, suffix: str) -> Path:
    """``Name.txt`` for entry ``Name`` - a name with dots keeps all of them."""
    return entry.parent / (entry.name + suffix)


def _media(drawer: Path, entry: Path, header, layout: Layout) -> None:
    """The entry's text, and its screenshot where the layout can show it."""
    if header is not None:
        name, copyright_, info = header
        lines = []
        for paragraph in [name, copyright_] + info.split("\n"):
            lines += textwrap.wrap(paragraph, layout.text_width) or [""]
        text = "\n".join(lines[:layout.text_height]).strip("\n")
        if text:
            _beside(entry, ".txt").write_text(text + "\n",
                                                 encoding="latin-1",
                                                 errors="replace")
    width, height, planes = layout.picture
    for picture in sorted(drawer.iterdir()):
        if picture.suffix.lower() not in IFF_EXTENSIONS:
            continue
        try:
            data = picture.read_bytes()
        except OSError:
            continue
        size = picture_size(data)
        if size and size[0] <= width and size[1] <= height \
                and size[2] <= planes:
            _beside(entry, ".iff").write_bytes(data)
            return
