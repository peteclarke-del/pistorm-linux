"""Reading ISO 9660 CD images, enough to install AmigaOS from one.

Workbench 3.5 and 3.9 were sold on CD rather than floppy, so the disks that
``amigaos.py`` knows how to read are not the material here: the release arrives
as an ISO 9660 image with the system laid out as a directory tree inside it.

Only reading is implemented, and only the parts these two discs actually use.
That is a much smaller job than ISO 9660 in general, but the two discs do not
agree with each other, so both dialects have to work:

* **AmigaOS 3.9** is plain ISO 9660 with upper-case names and the ``;1``
  version suffix - ``WORKBENCH3.9/LIBS/ICON.LIBRARY;1``.
* **AmigaOS 3.5** carries a Joliet supplementary descriptor *and* puts
  mixed-case names in the primary one, which is not what the standard says but
  is what was pressed - ``OS-Version3.5/Workbench/Libs``.

Names are read from whichever of the three sources the disc actually carries,
in this order: Joliet, then a Rock Ridge ``NM`` entry, then the plain ISO name.
Both extensions exist to say what the file is *really* called, and between them
the two discs need both - the 3.5 disc answers through Joliet, the 3.9 disc
through Rock Ridge.

Getting that wrong is not cosmetic.  The 3.9 disc's plain ISO names are upper
case, so a reader that stopped there would put ``AMIDOCK`` and ``DEFICONS`` in
WBStartup: Workbench draws an icon's label from the file name, so the desktop
would shout, and every startup line, tool type and icon this project retargets
would be matched against a spelling that was never on the disc.  ``AMIDOCK;1``
carries an ``NM`` entry reading ``AmiDock``, and that is the name used.
"""
from __future__ import annotations

import dataclasses
import struct
from pathlib import Path
from typing import BinaryIO, Iterator

SECTOR = 2048

#  Volume descriptor types, at the first byte of each descriptor.
VD_PRIMARY = 1
VD_SUPPLEMENTARY = 2
VD_TERMINATOR = 255

#  Descriptors start at sector 16 and run until the terminator.  A disc with no
#  terminator is malformed; stop anyway rather than reading the whole image.
FIRST_DESCRIPTOR = 16
MAX_DESCRIPTORS = 32

#  Directory record flags.
FLAG_HIDDEN = 0x01
FLAG_DIRECTORY = 0x02
FLAG_MULTI_EXTENT = 0x80

#  The escape sequences that mark a supplementary descriptor as Joliet: UCS-2
#  at levels 1, 2 and 3.  Anything else in that field is some other extension
#  and its names are not UTF-16, so it is left alone.
JOLIET_ESCAPES = (b"%/@", b"%/C", b"%/E")

#  A directory record names itself "\x00" and its parent "\x01".
SELF = b"\x00"
PARENT = b"\x01"

#  Rock Ridge, carried in the System Use area that follows the name.  Entries
#  are (signature, length, version, data...); "NM" is the alternate name and is
#  the only one this project needs.  Its flags say whether the name continues
#  in the next NM entry, or refers to "." or ".." rather than to a file.
NM_CONTINUE = 0x01
NM_CURRENT = 0x02
NM_PARENT = 0x04
SUSP_HEADER = 4


class Iso9660Error(Exception):
    """The image is not an ISO 9660 filesystem, or is damaged."""


@dataclasses.dataclass
class Entry:
    """One file or directory in the image."""
    name: str
    lba: int
    size: int
    flags: int
    #  Where the record itself lives, so a directory can be re-read without
    #  keeping every child in memory.
    is_dir: bool = False

    @property
    def offset(self) -> int:
        return self.lba * SECTOR


def _both32(data: bytes, offset: int) -> int:
    """Read a both-endian 32-bit field, taking the little-endian half.

    ISO 9660 stores these numbers twice, once each way, so that a reader of
    either endianness can pick the half it likes without swapping.
    """
    return struct.unpack_from("<I", data, offset)[0]


def _clean_name(raw: bytes, joliet: bool) -> str:
    """Turn a directory record's name field into the name a person would use."""
    if joliet:
        #  Joliet names are UTF-16 big-endian.  An odd length cannot be, so
        #  fall back rather than raising: a truncated name is still better
        #  than refusing to read the disc.
        try:
            name = raw.decode("utf-16-be")
        except UnicodeDecodeError:
            name = raw.decode("latin-1")
    else:
        #  Latin-1 rather than ASCII: the 3.5 disc has "Español.info" in its
        #  primary descriptor, and strict ASCII would refuse it.
        name = raw.decode("latin-1")
    #  ";1" is the ISO 9660 file version, and no Amiga file is called that.
    version = name.rfind(";")
    if version > 0:
        name = name[:version]
    #  A trailing dot is how ISO 9660 spells "no extension"; the Amiga's name
    #  for such a file has no dot on the end.
    if name.endswith(".") and len(name) > 1:
        name = name[:-1]
    return name


def _rock_ridge_name(system_use: bytes) -> str:
    """The Rock Ridge alternate name, or "" if the record has none.

    The System Use area is a flat run of SUSP entries.  A name may be split
    across several ``NM`` entries, each flagging that the next one continues
    it, so they are joined in the order they appear.
    """
    parts: list[str] = []
    offset = 0
    while offset + SUSP_HEADER <= len(system_use):
        signature = system_use[offset:offset + 2]
        length = system_use[offset + 2]
        #  A zero or undersized length would not advance, and the padding at
        #  the end of the area reads as exactly that.
        if length < SUSP_HEADER:
            break
        if signature == b"NM":
            flags = system_use[offset + 4]
            #  "." and ".." are not files and their names are never used.
            if flags & (NM_CURRENT | NM_PARENT):
                return ""
            parts.append(system_use[offset + 5:offset + length]
                         .decode("latin-1"))
            if not flags & NM_CONTINUE:
                break
        offset += length
    return "".join(parts)


class IsoImage:
    """An open ISO 9660 image.

    The handle is kept open for the life of the object, because the trees these
    discs carry are read file by file rather than all at once.
    """

    def __init__(self, handle: BinaryIO, *, prefer_joliet: bool = True):
        self.handle = handle
        self.volume_name = ""
        self.joliet = False
        self._root_record: bytes | None = None
        self._read_descriptors(prefer_joliet)

    # ------------------------------------------------------------ opening

    @classmethod
    def open(cls, path: str | Path, *, prefer_joliet: bool = True) -> "IsoImage":
        return cls(open(Path(path), "rb"), prefer_joliet=prefer_joliet)

    def close(self) -> None:
        self.handle.close()

    def __enter__(self) -> "IsoImage":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _read_descriptors(self, prefer_joliet: bool) -> None:
        primary: bytes | None = None
        supplementary: bytes | None = None
        for sector in range(FIRST_DESCRIPTOR,
                            FIRST_DESCRIPTOR + MAX_DESCRIPTORS):
            self.handle.seek(sector * SECTOR)
            data = self.handle.read(SECTOR)
            if len(data) < SECTOR or data[1:6] != b"CD001":
                break
            kind = data[0]
            if kind == VD_PRIMARY:
                primary = data
            elif kind == VD_SUPPLEMENTARY:
                #  Only a Joliet one is useful; another extension's names are
                #  not UTF-16 and reading them as such would be nonsense.
                escapes = data[88:120]
                if any(escapes.startswith(e) for e in JOLIET_ESCAPES):
                    supplementary = data
            elif kind == VD_TERMINATOR:
                break
        if primary is None:
            raise Iso9660Error("No ISO 9660 primary volume descriptor found")

        chosen, self.joliet = ((supplementary, True)
                               if (supplementary is not None and prefer_joliet)
                               else (primary, False))
        #  The volume name is always read from the primary descriptor: Joliet
        #  pads its own with NULs, and the primary is the one that matches what
        #  the disc is called everywhere else.
        self.volume_name = primary[40:72].decode("latin-1").rstrip(" \x00")
        self._root_record = chosen[156:190]

    # ------------------------------------------------------------ reading

    def root(self) -> Entry:
        assert self._root_record is not None
        return Entry(name="", lba=_both32(self._root_record, 2),
                     size=_both32(self._root_record, 10),
                     flags=FLAG_DIRECTORY, is_dir=True)

    def listdir(self, entry: Entry) -> list[Entry]:
        """The children of a directory, in the order the disc stores them."""
        if not entry.is_dir:
            raise Iso9660Error(f"{entry.name!r} is not a directory")
        self.handle.seek(entry.offset)
        data = self.handle.read(entry.size)
        out: list[Entry] = []
        offset = 0
        while offset < len(data):
            length = data[offset]
            if length == 0:
                #  A directory record never straddles a sector boundary, so a
                #  zero length means padding to the end of this sector.
                offset = (offset // SECTOR + 1) * SECTOR
                continue
            record = data[offset:offset + length]
            offset += length
            if len(record) < 33:
                continue
            name_len = record[32]
            raw = record[33:33 + name_len]
            if raw in (SELF, PARENT):
                continue
            flags = record[25]
            name = _clean_name(raw, self.joliet)
            #  Joliet already gives the real spelling, so Rock Ridge is only
            #  consulted when reading the primary descriptor - which is where
            #  the 3.9 disc keeps its names.  The System Use area begins after
            #  the name, padded to an even offset.
            if not self.joliet:
                start = 33 + name_len + (1 - name_len % 2)
                alternate = _rock_ridge_name(record[start:])
                if alternate:
                    name = alternate
            out.append(Entry(name=name,
                             lba=_both32(record, 2),
                             size=_both32(record, 10),
                             flags=flags,
                             is_dir=bool(flags & FLAG_DIRECTORY)))
        return out

    def find(self, path: str) -> Entry | None:
        """Look one path up, case-insensitively.

        Case-insensitively because the same tree is spelled two ways on the two
        discs - ``WORKBENCH3.9`` and ``OS-Version3.5`` - and a caller asking
        for a known directory should not have to know which disc it is on.
        """
        entry = self.root()
        for part in str(path).strip("/").split("/"):
            if not part:
                continue
            if not entry.is_dir:
                return None
            wanted = part.lower()
            for child in self.listdir(entry):
                if child.name.lower() == wanted:
                    entry = child
                    break
            else:
                return None
        return entry

    def walk(self, entry: Entry | None = None,
             prefix: str = "") -> Iterator[tuple[str, Entry]]:
        """Every file under a directory, as (relative path, entry)."""
        if entry is None:
            entry = self.root()
        for child in self.listdir(entry):
            relative = f"{prefix}{child.name}"
            if child.is_dir:
                yield relative, child
                yield from self.walk(child, relative + "/")
            else:
                yield relative, child

    def read(self, entry: Entry) -> bytes:
        """The contents of one file."""
        if entry.is_dir:
            raise Iso9660Error(f"{entry.name!r} is a directory")
        self.handle.seek(entry.offset)
        return self.handle.read(entry.size)

    def extract(self, entry: Entry, into: str | Path,
                chunk: int = 1 << 20) -> Path:
        """Write one file out, without holding it all in memory."""
        destination = Path(into)
        destination.parent.mkdir(parents=True, exist_ok=True)
        remaining = entry.size
        self.handle.seek(entry.offset)
        with open(destination, "wb") as out:
            while remaining > 0:
                data = self.handle.read(min(chunk, remaining))
                if not data:
                    break
                out.write(data)
                remaining -= len(data)
        return destination


def is_iso(path: str | Path) -> bool:
    """Whether this file looks like an ISO 9660 image.

    By reading the descriptor rather than the suffix: the discs in circulation
    are named ``.iso``, but so is every other kind of disc image, and a card
    image with an ``.iso`` name would otherwise be offered as a CD.
    """
    try:
        with open(Path(path), "rb") as handle:
            handle.seek(FIRST_DESCRIPTOR * SECTOR)
            return handle.read(6)[1:6] == b"CD001"
    except OSError:
        return False
