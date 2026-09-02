"""Reading PFS3 volumes (Professional File System 3).

PFS3 is what every large Amiga partition in practice uses, because Kickstart's
own FFS is slow and unreliable at those sizes.  It is not in ROM, so its handler
has to be embedded in the RDB - see :mod:`pistorm_imager.core.rdb`.

Structure definitions follow ``pfs3.h`` from ``tonioni/pfsdoctor`` and the
reference implementation in ``tonioni/pfs3aio``.  Two details are easy to get
wrong by assumption and are worth stating:

* **Reserved blocks are not the same size as data blocks.**  Metadata lives in
  blocks of ``reserved_blksize`` bytes (1024 on every real volume examined)
  while file data uses 512-byte blocks.  Reserved block *numbers* are 512-byte
  sector numbers, and each reserved block spans ``reserved_blksize / 512``
  sectors.
* **Files are extent based.**  An ``anode`` describes ``clustersize``
  consecutive blocks starting at ``blocknr``, and chains to the next anode.
  There is no per-block pointer list as in FFS.
"""
from __future__ import annotations

import dataclasses
import struct
from typing import BinaryIO, Iterator

SECTOR = 512

DISKTYPE_PFS3 = 0x50465301          # 'PFS\1' - what a formatted volume carries

# rootblock.options
MODE_HARDDISK = 1
MODE_SPLITTED_ANODES = 2
MODE_DIR_EXTENSION = 4
MODE_DELDIR = 8
MODE_SIZEFIELD = 16
MODE_EXTENSION = 32
MODE_DATESTAMP = 64
MODE_SUPERINDEX = 128
MODE_SUPERDELDIR = 256
MODE_EXTROVING = 512
MODE_LONGFN = 1024
MODE_LARGEFILE = 2048
MODE_STORED_GEOM = 4096

MODE_NAMES = [
    (MODE_HARDDISK, "HARDDISK"), (MODE_SPLITTED_ANODES, "SPLITTED_ANODES"),
    (MODE_DIR_EXTENSION, "DIR_EXTENSION"), (MODE_DELDIR, "DELDIR"),
    (MODE_SIZEFIELD, "SIZEFIELD"), (MODE_EXTENSION, "EXTENSION"),
    (MODE_DATESTAMP, "DATESTAMP"), (MODE_SUPERINDEX, "SUPERINDEX"),
    (MODE_SUPERDELDIR, "SUPERDELDIR"), (MODE_EXTROVING, "EXTROVING"),
    (MODE_LONGFN, "LONGFN"), (MODE_LARGEFILE, "LARGEFILE"),
    (MODE_STORED_GEOM, "STORED_GEOM"),
]

# Reserved anode numbers
ANODE_EOF = 0
ANODE_BADBLOCKS = 4
ANODE_ROOTDIR = 5
ANODE_USERFIRST = 6
#  What a real volume stores in the reserved anodes 0..4 to mark them taken.
#  A free anode is one whose blocknr is zero, so leaving them cleared would
#  offer the handler ANODE_BADBLOCKS and friends as spare anodes.
ANODE_RESERVED_BLOCKNR = 0xFFFFFFFF

BOOT_BLOCK = 0
ROOT_BLOCK = 2                      # sector number of the root block

# Amiga secondary types, as stored in a direntry
ST_USERDIR = 2
ST_FILE = -3

SIZEOF_ANODEBLOCK_HEADER = 16       # id, pad, datestamp, seqnr, pad
SIZEOF_INDEXBLOCK_HEADER = 12       # id, pad, datestamp, seqnr
SIZEOF_ANODE = 12                   # clustersize, blocknr, next
SIZEOF_DIRBLOCK_HEADER = 20         # id, pad, datestamp, pad[2], anodenr, parent

#  Block identifiers, from blocks.h in the reference implementation.  Note that
#  the comments in pfs3.h claim 'AI' and 'BI'; the real constants are 'IB' and
#  'MI', which only reading an actual volume reveals.
ID_DIRBLOCK = b"DB"
ID_ANODEBLOCK = b"AB"
ID_INDEXBLOCK = b"IB"
ID_BITMAPBLOCK = b"BM"
ID_BITMAPINDEX = b"MI"
ID_SUPERBLOCK = b"SB"
ID_EXTENSION = b"EX"


class Pfs3Error(RuntimeError):
    pass


def mode_names(options: int) -> list[str]:
    return [name for value, name in MODE_NAMES if options & value]


@dataclasses.dataclass
class Anode:
    clustersize: int
    blocknr: int
    next: int


@dataclasses.dataclass
class Entry:
    name: str
    anode: int
    is_dir: bool
    size: int = 0
    protection: int = 0
    comment: str = ""
    days: int = 0
    mins: int = 0
    ticks: int = 0

    @property
    def is_file(self) -> bool:
        return not self.is_dir

    @property
    def protect(self) -> int:
        """Alias so FFS and PFS3 entries can be handled by the same code."""
        return self.protection


class Pfs3Volume:
    """Read-only access to a PFS3 volume at ``offset`` inside ``handle``."""

    def __init__(self, handle: BinaryIO, offset: int = 0):
        self.f = handle
        self.base = offset
        self._read_root()

    # ------------------------------------------------------------ low level

    def _sectors(self, sector: int, count: int) -> bytes:
        self.f.seek(self.base + sector * SECTOR)
        data = self.f.read(count * SECTOR)
        if len(data) != count * SECTOR:
            raise Pfs3Error(f"short read at sector {sector}")
        return data

    def _read_root(self) -> None:
        boot = self._sectors(BOOT_BLOCK, 1)
        if struct.unpack_from(">I", boot, 0)[0] != DISKTYPE_PFS3:
            raise Pfs3Error("not a PFS3 volume (no PFS\\1 in the boot block)")

        head = self._sectors(ROOT_BLOCK, 1)
        if struct.unpack_from(">I", head, 0)[0] != DISKTYPE_PFS3:
            raise Pfs3Error("no PFS3 root block at sector 2")
        self.options = struct.unpack_from(">I", head, 4)[0]
        self.reserved_blksize = struct.unpack_from(">H", head, 64)[0] or 1024
        self.rescluster = max(1, self.reserved_blksize // SECTOR)

        root = self._sectors(ROOT_BLOCK, self.rescluster)
        self.datestamp = struct.unpack_from(">I", root, 8)[0]
        name_len = min(root[20], 31)
        self.name = root[21:21 + name_len].decode("latin-1")
        self.lastreserved, self.firstreserved, self.reserved_free = \
            struct.unpack_from(">III", root, 52)
        self.rblkcluster = struct.unpack_from(">H", root, 66)[0]
        (self.blocksfree, self.alwaysfree, self.roving_ptr, self.deldir,
         self.disksize, self.extension) = struct.unpack_from(">IIIIII", root, 68)

        self.fnsize = 32
        self.split_anodes = bool(self.options & MODE_SPLITTED_ANODES)
        self.superindex = bool(self.options & MODE_SUPERINDEX)
        self.dir_extension = bool(self.options & MODE_DIR_EXTENSION)
        self.anodes_per_block = (self.reserved_blksize
                                 - SIZEOF_ANODEBLOCK_HEADER) // SIZEOF_ANODE
        self.index_per_block = (self.reserved_blksize
                                - SIZEOF_INDEXBLOCK_HEADER) // 4

        #  A ``not_used`` long sits between ``extension`` and the index union,
        #  so the union starts at 96 - reading it at 92 yields a spurious
        #  leading zero and every lookup then misses by one slot.
        idx = 96
        #  The extension block carries the filename limit for every volume, not
        #  only the ones large enough to need a super index.
        extension_block = None
        if self.extension:
            try:
                candidate = self._sectors(self.extension, self.rescluster)
                if candidate[0:2] == ID_EXTENSION:
                    extension_block = candidate
                    self.fnsize = struct.unpack_from(">H", candidate, 56)[0] or 32
            except Pfs3Error:
                extension_block = None

        if self.superindex:
            self.bitmap_index = list(struct.unpack_from(">104I", root, idx))
            self.index_blocks: list[int] = []
            self.super_index: list[int] = []
            if extension_block is not None:
                #  superindex[16] follows the three not_used_2 words, at 64.
                self.super_index = list(
                    struct.unpack_from(">16I", extension_block, 64))
        else:
            self.bitmap_index = list(struct.unpack_from(">5I", root, idx))
            self.index_blocks = list(struct.unpack_from(">99I", root, idx + 20))
            self.super_index = []

    def _reserved(self, sector: int) -> bytes:
        return self._sectors(sector, self.rescluster)

    # --------------------------------------------------------------- anodes

    def _index_block(self, number: int) -> list[int]:
        """Return the index block that maps anode-block sequence numbers."""
        if self.superindex:
            super_nr, offset = divmod(number, self.index_per_block)
            if super_nr >= len(self.super_index) or not self.super_index[super_nr]:
                raise Pfs3Error(f"super index {super_nr} is empty")
            block = self._reserved(self.super_index[super_nr])
            if block[0:2] != ID_SUPERBLOCK:
                raise Pfs3Error(f"expected a super block, got {block[0:2]!r}")
            pointer = struct.unpack_from(">I", block,
                                         SIZEOF_INDEXBLOCK_HEADER + offset * 4)[0]
        else:
            if number >= len(self.index_blocks):
                raise Pfs3Error(f"index block {number} is out of range")
            pointer = self.index_blocks[number]
        if not pointer:
            raise Pfs3Error(f"index block {number} is unallocated")
        block = self._reserved(pointer)
        if block[0:2] != ID_INDEXBLOCK:
            raise Pfs3Error(f"expected an index block, got {block[0:2]!r}")
        return list(struct.unpack_from(f">{self.index_per_block}I", block,
                                       SIZEOF_INDEXBLOCK_HEADER))

    def _anode_block(self, seqnr: int) -> bytes:
        index_nr, offset = divmod(seqnr, self.index_per_block)
        pointer = self._index_block(index_nr)[offset]
        if not pointer:
            raise Pfs3Error(f"anode block {seqnr} is unallocated")
        block = self._reserved(pointer)
        if block[0:2] != ID_ANODEBLOCK:
            raise Pfs3Error(f"expected an anode block, got {block[0:2]!r}")
        return block

    def anode(self, number: int) -> Anode:
        """Look up one anode - PFS3's extent descriptor."""
        if self.split_anodes:
            #  The anode number is two 16-bit halves, not a division.
            seqnr, offset = number >> 16, number & 0xFFFF
        else:
            seqnr, offset = divmod(number, self.anodes_per_block)
        if offset >= self.anodes_per_block:
            raise Pfs3Error(f"anode offset {offset} out of range")
        block = self._anode_block(seqnr)
        at = SIZEOF_ANODEBLOCK_HEADER + offset * SIZEOF_ANODE
        clustersize, blocknr, nxt = struct.unpack_from(">III", block, at)
        return Anode(clustersize, blocknr, nxt)

    def anode_chain(self, number: int) -> Iterator[Anode]:
        seen: set[int] = set()
        while number and number != ANODE_EOF:
            if number in seen:
                raise Pfs3Error("cyclic anode chain")
            seen.add(number)
            node = self.anode(number)
            yield node
            number = node.next

    # ---------------------------------------------------------- directories

    def _parse_entries(self, block: bytes) -> Iterator[Entry]:
        offset = SIZEOF_DIRBLOCK_HEADER
        limit = self.reserved_blksize
        while offset < limit:
            size = block[offset]
            if size == 0:
                return
            if offset + size > limit:
                return
            entry_type = struct.unpack_from(">b", block, offset + 1)[0]
            anode, fsize = struct.unpack_from(">II", block, offset + 2)
            days, mins, ticks = struct.unpack_from(">HHH", block, offset + 10)
            protection = block[offset + 16]
            nlength = block[offset + 17]
            name = block[offset + 18:offset + 18 + nlength].decode("latin-1")
            after = offset + 18 + nlength
            comment = ""
            if after < offset + size:
                clength = block[after]
                if clength and after + 1 + clength <= offset + size:
                    comment = block[after + 1:after + 1 + clength].decode("latin-1")
            yield Entry(
                name=name, anode=anode, is_dir=entry_type >= 0, size=fsize,
                protection=protection, comment=comment,
                days=days, mins=mins, ticks=ticks)
            offset += size

    def listdir(self, anode_number: int = ANODE_ROOTDIR) -> list[Entry]:
        """Entries of a directory, following its chain of directory blocks."""
        entries: list[Entry] = []
        for node in self.anode_chain(anode_number):
            for index in range(node.clustersize):
                block = self._reserved(node.blocknr + index * self.rescluster)
                if block[0:2] != ID_DIRBLOCK:
                    continue
                entries += list(self._parse_entries(block))
        return sorted(entries, key=lambda e: e.name.lower())

    def walk(self, anode_number: int = ANODE_ROOTDIR,
             path: str = "") -> Iterator[tuple[str, Entry]]:
        for entry in self.listdir(anode_number):
            full = f"{path}/{entry.name}" if path else entry.name
            yield full, entry
            if entry.is_dir:
                yield from self.walk(entry.anode, full)

    def read_file(self, entry: Entry) -> bytes:
        """Read a file by walking its anode chain of extents."""
        out = bytearray()
        for node in self.anode_chain(entry.anode):
            if not node.blocknr:
                continue
            self.f.seek(self.base + node.blocknr * SECTOR)
            out += self.f.read(node.clustersize * SECTOR)
            if len(out) >= entry.size:
                break
        if len(out) < entry.size:
            raise Pfs3Error(
                f"{entry.name}: expected {entry.size} bytes, recovered {len(out)}")
        return bytes(out[:entry.size])

    def find(self, path: str) -> Entry | None:
        current = ANODE_ROOTDIR
        entry: Entry | None = None
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            entry = next((e for e in self.listdir(current)
                          if e.name.lower() == part.lower()), None)
            if entry is None:
                return None
            current = entry.anode
        return entry

    @property
    def max_name_length(self) -> int:
        return max(1, self.fnsize - 1)

    def describe(self) -> str:
        from .util import human_size
        return (f'"{self.name}" PFS3, {human_size(self.disksize * SECTOR)}, '
                f"{human_size(self.blocksfree * SECTOR)} free, "
                f"{self.reserved_blksize}-byte reserved blocks "
                f"[{', '.join(mode_names(self.options))}]")


def clear_file_data(volume: "Pfs3Volume", path: str) -> int:
    """Blank a file's contents in place, leaving everything else alone.

    The writer here is create-and-fill: it cannot delete a file or change a
    directory once written.  Overwriting the data of an existing file needs
    none of that - the extents are already allocated and stay exactly as they
    are - so a file can be neutralised on a finished volume without touching a
    single piece of metadata.

    Returns the number of bytes blanked, or 0 if the file was not found.  The
    handle must have been opened for writing.
    """
    entry = volume.find(path)
    if entry is None or entry.is_dir:
        return 0
    blanked = 0
    for node in volume.anode_chain(entry.anode):
        if not node.blocknr or not node.clustersize:
            continue
        volume.f.seek(volume.base + node.blocknr * SECTOR)
        volume.f.write(b"\0" * (node.clustersize * SECTOR))
        blanked += node.clustersize * SECTOR
    volume.f.flush()
    return min(blanked, entry.size)


# ---------------------------------------------------------------- writing

VERNUM, REVNUM = 19, 2              # the handler version we claim compatibility with
#  PFS3 reads its filename limit from the volume itself (rootblock extension
#  "fnsize"); the handler is built for FNSIZE 108.  pfs3aio's own formatter
#  writes a conservative 32, which would force names to be shortened - and a
#  renamed file breaks the WHDLoad slave or tool type that refers to it.
MAX_FNSIZE = 107
DEFAULT_FNSIZE = MAX_FNSIZE
MAXSMALLBITMAPINDEX = 4
MAXBITMAPINDEX = 103
MAXNUMRESERVED = 4096 + 255 * 1024 * 8
DNSIZE = 32                          # disk name field

#  A freshly formatted volume carries exactly this mode set, which is what
#  format.c in the reference implementation produces - and what every real
#  volume examined actually has (0x77f, or 0x7ff once SUPERINDEX is added).
FORMAT_OPTIONS = (MODE_HARDDISK | MODE_SPLITTED_ANODES | MODE_DIR_EXTENSION
                  | MODE_SIZEFIELD | MODE_DATESTAMP | MODE_EXTROVING
                  | MODE_LONGFN | MODE_EXTENSION | MODE_DELDIR
                  | MODE_SUPERDELDIR)


def bitmap_payload(reserved_blksize: int) -> int:
    """Longs of bitmap carried by one bitmap block."""
    return reserved_blksize // 4 - 3


def max_small_disk(reserved_blksize: int = 1024) -> int:
    payload = bitmap_payload(reserved_blksize)
    return (MAXSMALLBITMAPINDEX + 1) * payload * payload * 32


def calc_num_reserved(total_sectors: int, reserved_blksize: int) -> int:
    """Reserved blocks a volume of this size needs (format.c CalcNumReserved)."""
    taken = 32
    i = 2048
    while i and i // 2 < total_sectors:
        taken += taken * (10 if i >= 512 * 2048 else 14) // 16
        i <<= 1
    taken //= reserved_blksize // 1024
    taken = min(MAXNUMRESERVED, taken - 1)
    return (taken + 31) & ~0x1F         # always a multiple of 32


class Pfs3Writer:
    """Formats a PFS3 volume and fills it once.

    Deliberately create-and-fill only: blocks and anodes are handed out
    sequentially and never returned, which is all an imager needs and avoids
    reimplementing PFS3's allocator.
    """

    def __init__(self, handle: BinaryIO, offset: int, total_sectors: int,
                 name: str, reserved_blksize: int = 1024,
                 fnsize: int = DEFAULT_FNSIZE):
        if total_sectors < 1024:
            raise Pfs3Error("volume is too small for PFS3")
        self.f = handle
        self.base = offset
        self.total_sectors = total_sectors
        self.name = name[:DNSIZE - 2]
        self.fnsize = max(32, min(int(fnsize), MAX_FNSIZE))

        self.reserved_blksize = reserved_blksize
        self.rescluster = reserved_blksize // SECTOR
        self.options = FORMAT_OPTIONS
        if total_sectors > max_small_disk(reserved_blksize):
            self.options |= MODE_SUPERINDEX
        self.superindex = bool(self.options & MODE_SUPERINDEX)

        self.anodes_per_block = (reserved_blksize
                                 - SIZEOF_ANODEBLOCK_HEADER) // SIZEOF_ANODE
        self.index_per_block = (reserved_blksize
                                - SIZEOF_INDEXBLOCK_HEADER) // 4
        self.longs_per_bmb = bitmap_payload(reserved_blksize)

        self.num_reserved = calc_num_reserved(total_sectors, reserved_blksize)
        self.first_reserved = ROOT_BLOCK
        self.last_reserved = (self.rescluster * self.num_reserved
                              + self.first_reserved - 1)

        #  Reserved-area bitmap: one bit per reserved block, MSB first.
        self._res_free = bytearray(b"\1" * self.num_reserved)
        self._res_rover = 0
        #  Data area
        self.bitmap_start = self.last_reserved + 1
        self.data_blocks = total_sectors - self.bitmap_start
        self._data_rover = 0
        self._data_used = 0

        self.index_blocks: list[int] = []      # 'IB' block numbers by seqnr
        self.super_blocks: list[int] = []
        self.bitmap_blocks: list[int] = []
        self.bitmap_index_blocks: list[int] = []
        self._anode_blocks: dict[int, int] = {}
        self._next_anode = 0
        self.root_anode = 0

    # ------------------------------------------------------------ raw access

    def _write_sectors(self, sector: int, data: bytes) -> None:
        if sector + len(data) // SECTOR > self.total_sectors:
            raise Pfs3Error("write past the end of the volume")
        self.f.seek(self.base + sector * SECTOR)
        self.f.write(data)

    def _reserved_sector(self, index: int) -> int:
        """Sector number of reserved block ``index`` (0 is the root block)."""
        return self.first_reserved + index * self.rescluster

    def alloc_reserved(self) -> int:
        """Take the next free reserved block, returning its sector number."""
        while self._res_rover < self.num_reserved:
            index = self._res_rover
            self._res_rover += 1
            if self._res_free[index]:
                self._res_free[index] = 0
                return self._reserved_sector(index)
        raise Pfs3Error("out of reserved blocks")

    def alloc_data(self, count: int) -> int:
        """Take ``count`` consecutive data blocks, returning the first."""
        if self._data_rover + count > self.data_blocks:
            raise Pfs3Error("the PFS3 volume is full")
        start = self.bitmap_start + self._data_rover
        self._data_rover += count
        self._data_used += count
        return start

    @property
    def free_bytes(self) -> int:
        return (self.data_blocks - self._data_used) * SECTOR

    @property
    def max_name_length(self) -> int:
        """Longest file name this volume accepts."""
        return self.fnsize - 1

    def _new_reserved_block(self, block_id: bytes, seqnr: int = 0) -> tuple[int, bytearray]:
        sector = self.alloc_reserved()
        block = bytearray(self.reserved_blksize)
        block[0:2] = block_id
        struct.pack_into(">I", block, 4, 1)        # datestamp
        struct.pack_into(">I", block, 8, seqnr)
        return sector, block

    # ---------------------------------------------------------------- anodes

    def _anode_block_for(self, seqnr: int) -> int:
        """Sector of the anode block for ``seqnr``, creating it if needed."""
        if seqnr in self._anode_blocks:
            return self._anode_blocks[seqnr]
        sector, block = self._new_reserved_block(ID_ANODEBLOCK, seqnr)
        self._write_sectors(sector, bytes(block))
        self._anode_blocks[seqnr] = sector
        #  Record it in the index so the handler can find it again.
        index_nr, offset = divmod(seqnr, self.index_per_block)
        index_sector = self._index_block_for(index_nr)
        block = bytearray(self._read_reserved(index_sector))
        struct.pack_into(">I", block, SIZEOF_INDEXBLOCK_HEADER + offset * 4, sector)
        self._write_sectors(index_sector, bytes(block))
        return sector

    def _index_block_for(self, index_nr: int) -> int:
        """Sector of anode index block ``index_nr``, creating it if needed.

        A small volume lists its index blocks directly in the root block.  Past
        :func:`max_small_disk` the root block's index array is given over to
        the bitmap instead, and the handler reaches the anode index through a
        level of 'SB' super blocks named by the root block extension.  A large
        volume whose super blocks are left unwritten therefore has an anode
        index the handler cannot see at all: it refuses to mount, reporting
        "Anode index invalid" and then "Disk update failed".
        """
        while len(self.index_blocks) <= index_nr:
            self.index_blocks.append(0)
        if not self.index_blocks[index_nr]:
            sector, block = self._new_reserved_block(ID_INDEXBLOCK, index_nr)
            self._write_sectors(sector, bytes(block))
            self.index_blocks[index_nr] = sector
            if self.superindex:
                super_nr, offset = divmod(index_nr, self.index_per_block)
                super_sector = self._super_block_for(super_nr)
                super_block = bytearray(self._read_reserved(super_sector))
                struct.pack_into(">I", super_block,
                                 SIZEOF_INDEXBLOCK_HEADER + offset * 4, sector)
                self._write_sectors(super_sector, bytes(super_block))
        return self.index_blocks[index_nr]

    def _super_block_for(self, super_nr: int) -> int:
        """Sector of super block ``super_nr``, creating it if needed."""
        if super_nr >= 16:
            raise Pfs3Error("volume needs more super blocks than PFS3 allows")
        while len(self.super_blocks) <= super_nr:
            self.super_blocks.append(0)
        if not self.super_blocks[super_nr]:
            sector, block = self._new_reserved_block(ID_SUPERBLOCK, super_nr)
            self._write_sectors(sector, bytes(block))
            self.super_blocks[super_nr] = sector
        return self.super_blocks[super_nr]

    def _read_reserved(self, sector: int) -> bytes:
        self.f.seek(self.base + sector * SECTOR)
        return self.f.read(self.reserved_blksize)

    def alloc_anode(self) -> int:
        """Allocate the next anode number (split form: seqnr<<16 | offset)."""
        number = self._next_anode
        self._next_anode += 1
        seqnr, offset = divmod(number, self.anodes_per_block)
        self._anode_block_for(seqnr)
        return (seqnr << 16) | offset

    def set_anode(self, anodenr: int, clustersize: int, blocknr: int,
                  nxt: int = ANODE_EOF) -> None:
        seqnr, offset = anodenr >> 16, anodenr & 0xFFFF
        sector = self._anode_block_for(seqnr)
        block = bytearray(self._read_reserved(sector))
        at = SIZEOF_ANODEBLOCK_HEADER + offset * SIZEOF_ANODE
        struct.pack_into(">III", block, at, clustersize, blocknr, nxt)
        self._write_sectors(sector, bytes(block))

    # ---------------------------------------------------------------- bitmap

    def _data_bitmap_blocks(self) -> int:
        """Bitmap blocks the volume needs.

        The bitmap covers the whole partition, not just the data area: bit
        ``n`` is block ``n`` counted from the start of the volume, so the
        reserved blocks occupy the first bits and are simply marked as used.
        Sizing it from the data area instead leaves it short by however many
        blocks the reserved area takes, and the handler - which works out the
        count from ``disksize`` - then follows a null pointer for the last
        stretch of the disk.  It is only harmless on a volume small enough for
        the two counts to round to the same number.
        """
        longs = (self.total_sectors + 31) // 32
        return (longs + self.longs_per_bmb - 1) // self.longs_per_bmb

    def _allocate_data_bitmap(self) -> None:
        """Reserve the bitmap blocks and the index blocks that point at them.

        Only the space is claimed here.  The bits themselves are filled in by
        :meth:`_fill_data_bitmap` at close, because during ``format`` no file
        has been written yet and the bitmap would permanently claim that the
        whole data area is free.
        """
        for seqnr in range(self._data_bitmap_blocks()):
            sector, block = self._new_reserved_block(ID_BITMAPBLOCK, seqnr)
            self._write_sectors(sector, bytes(block))
            self.bitmap_blocks.append(sector)

        #  Point at them through bitmap index blocks.
        for seqnr, sector in enumerate(self.bitmap_blocks):
            index_nr, offset = divmod(seqnr, self.index_per_block)
            while len(self.bitmap_index_blocks) <= index_nr:
                self.bitmap_index_blocks.append(0)
            if not self.bitmap_index_blocks[index_nr]:
                idx_sector, idx_block = self._new_reserved_block(ID_BITMAPINDEX,
                                                                 index_nr)
                self._write_sectors(idx_sector, bytes(idx_block))
                self.bitmap_index_blocks[index_nr] = idx_sector
            idx_sector = self.bitmap_index_blocks[index_nr]
            block = bytearray(self._read_reserved(idx_sector))
            struct.pack_into(">I", block,
                             SIZEOF_INDEXBLOCK_HEADER + offset * 4, sector)
            self._write_sectors(idx_sector, bytes(block))

    def _fill_data_bitmap(self) -> None:
        """Write the bits, now that allocation has finished.

        A set bit means *free*, and bit 0 of each long is the most significant
        bit - the opposite convention to FFS.  Bit ``n`` is block ``n`` of the
        partition, so everything below the first unallocated data block is
        marked used: the boot block, the whole reserved area, and every block
        handed out to a file.
        """
        payload = self.longs_per_bmb * 4
        first_free = self.bitmap_start + self._data_used
        bits = bytearray(len(self.bitmap_blocks) * payload)
        #  One contiguous run of free blocks, so set it a byte at a time
        #  rather than a bit at a time - a big volume has tens of millions.
        for index in range(first_free, min(first_free + (-first_free) % 8,
                                           self.total_sectors)):
            bits[index // 8] |= 0x80 >> (index % 8)
        start_byte = (first_free + 7) // 8
        end_byte = self.total_sectors // 8
        if end_byte > start_byte:
            bits[start_byte:end_byte] = b"\xff" * (end_byte - start_byte)
        for index in range(max(first_free, end_byte * 8), self.total_sectors):
            bits[index // 8] |= 0x80 >> (index % 8)

        for seqnr, sector in enumerate(self.bitmap_blocks):
            block = bytearray(self._read_reserved(sector))
            chunk = bits[seqnr * payload:(seqnr + 1) * payload]
            block[SIZEOF_INDEXBLOCK_HEADER:
                  SIZEOF_INDEXBLOCK_HEADER + payload] = chunk
            self._write_sectors(sector, bytes(block))

    # ------------------------------------------------------------ formatting

    def format(self) -> None:
        """Lay down an empty PFS3 volume."""
        #  Boot block.
        boot = bytearray(2 * SECTOR)
        struct.pack_into(">I", boot, 0, DISKTYPE_PFS3)
        self._write_sectors(BOOT_BLOCK, bytes(boot))

        #  Reserved bitmap sits immediately behind the root block, so the root
        #  block "cluster" is however many reserved blocks the two need.
        chunks = 1
        i = 125
        while i < self.num_reserved // 32:
            chunks += 1
            i += 256
        numblocks = (1024 * chunks + self.reserved_blksize - 1) // self.reserved_blksize
        self.rblkcluster = self.rescluster * numblocks
        self.reserved_free = self.num_reserved - numblocks - 1

        #  The root block cluster and the extension block that follows it are
        #  taken; hand out everything after them.
        self._res_rover = numblocks + 1
        for index in range(numblocks + 1):
            self._res_free[index] = 0
        self.extension = self.first_reserved + self.rblkcluster

        self._allocate_data_bitmap()

        #  Anodes 0..4 are reserved; the root directory is always anode 5.
        for _ in range(ANODE_ROOTDIR):
            self.set_anode(self.alloc_anode(), 0, ANODE_RESERVED_BLOCKNR)
        self.root_anode = self.alloc_anode()
        root_dir_sector = self.alloc_reserved()
        self._write_dirblock(root_dir_sector, self.root_anode, parent=0)
        self.set_anode(self.root_anode, 1, root_dir_sector)

        self._write_extension()
        self._write_root()

    def _write_dirblock(self, sector: int, anodenr: int, parent: int) -> None:
        block = bytearray(self.reserved_blksize)
        block[0:2] = ID_DIRBLOCK
        struct.pack_into(">I", block, 4, 1)             # datestamp
        struct.pack_into(">II", block, 12, anodenr, parent)
        self._write_sectors(sector, bytes(block))

    def _write_extension(self) -> None:
        block = bytearray(self.reserved_blksize)
        block[0:2] = ID_EXTENSION
        struct.pack_into(">I", block, 8, 1)             # datestamp
        struct.pack_into(">I", block, 12, (VERNUM << 16) + REVNUM)
        struct.pack_into(">H", block, 56, self.fnsize)
        if self.superindex:
            for index, sector in enumerate(self.super_blocks[:16]):
                struct.pack_into(">I", block, 64 + index * 4, sector)
        self._write_sectors(self.extension, bytes(block))

    def _write_root(self) -> None:
        cluster = bytearray(self.rblkcluster * SECTOR)
        struct.pack_into(">II", cluster, 0, DISKTYPE_PFS3, self.options)
        struct.pack_into(">I", cluster, 8, 1)           # datestamp
        struct.pack_into(">HHHH", cluster, 12, 0, 0, 0, 0xF0)
        raw = self.name.encode("latin-1", errors="replace")[:DNSIZE - 2]
        cluster[20] = len(raw)
        cluster[21:21 + len(raw)] = raw
        #  Count what is genuinely still free rather than trusting the estimate
        #  made before any metadata blocks were handed out.
        reserved_free = sum(self._res_free)
        struct.pack_into(">III", cluster, 52,
                         self.last_reserved, self.first_reserved, reserved_free)
        struct.pack_into(">HH", cluster, 64, self.reserved_blksize, self.rblkcluster)
        blocksfree = self.data_blocks - self._data_used
        struct.pack_into(">IIIIII", cluster, 68,
                         blocksfree, blocksfree // 20, 0, 0,
                         self.total_sectors, self.extension)

        idx = 96
        if self.superindex:
            #  Bitmap index pointers live in the large array; anode index
            #  blocks are reached through super blocks in the extension.
            for index, sector in enumerate(self.bitmap_index_blocks[:104]):
                struct.pack_into(">I", cluster, idx + index * 4, sector)
        else:
            for index, sector in enumerate(self.bitmap_index_blocks[:5]):
                struct.pack_into(">I", cluster, idx + index * 4, sector)
            for index, sector in enumerate(self.index_blocks[:99]):
                struct.pack_into(">I", cluster, idx + 20 + index * 4, sector)

        #  Reserved-area bitmap, directly behind the 512-byte root block.
        at = SECTOR
        cluster[at:at + 2] = ID_BITMAPBLOCK
        struct.pack_into(">I", cluster, at + 4, 1)
        struct.pack_into(">I", cluster, at + 8, 0)
        bitmap_at = at + SIZEOF_INDEXBLOCK_HEADER
        for index in range(self.num_reserved):
            if not self._res_free[index]:
                continue
            long_index, bit = divmod(index, 32)
            position = bitmap_at + long_index * 4
            if position + 4 > len(cluster):
                break
            value = struct.unpack_from(">I", cluster, position)[0]
            struct.pack_into(">I", cluster, position, value | (0x80000000 >> bit))
        self._write_sectors(self.first_reserved, bytes(cluster))

    def close(self) -> None:
        self._fill_data_bitmap()
        #  Super blocks can be added long after ``format`` wrote the extension
        #  block, so the extension is written again from what actually exists.
        self._write_extension()
        self._write_root()
        self.f.flush()

    # ------------------------------------------------------- entry creation

    def _direntry(self, name: str, anodenr: int, is_dir: bool, size: int,
                  protection: int = 0, comment: str = "",
                  days: int = 0, mins: int = 0, ticks: int = 0) -> bytes:
        raw = name.encode("latin-1", errors="replace")[:self.max_name_length]
        note = comment.encode("latin-1", errors="replace")[:79]
        body = bytearray(18)
        struct.pack_into(">b", body, 1, ST_USERDIR if is_dir else ST_FILE)
        struct.pack_into(">II", body, 2, anodenr, 0 if is_dir else size)
        struct.pack_into(">HHH", body, 10, days, mins, ticks)
        body[16] = protection & 0xFF
        body[17] = len(raw)
        body += raw
        body += bytes([len(note)]) + note
        if len(body) % 2:
            body += b"\0"                 # the comment area is word aligned
        #  MODE_DIR_EXTENSION puts an "extra fields" bitmask in the last two
        #  bytes of every entry, and the handler reads it by stepping back from
        #  the end of the entry.  Omit it and the last two bytes of the name
        #  are read as that bitmask instead - harmless for an even-length name,
        #  where those bytes are the zero comment length and its padding, but
        #  an odd-length name puts its final character in the high byte and the
        #  handler then reconstructs fields nobody wrote.
        body += b"\0\0"
        body[0] = len(body)
        if len(body) > 255:
            raise Pfs3Error(f"directory entry for {name!r} is too long")
        return bytes(body)

    def _chain(self, anodenr: int) -> list[Anode]:
        out: list[Anode] = []
        number = anodenr
        while number and number != ANODE_EOF:
            seqnr, offset = number >> 16, number & 0xFFFF
            block = self._read_reserved(self._anode_block_for(seqnr))
            at = SIZEOF_ANODEBLOCK_HEADER + offset * SIZEOF_ANODE
            clustersize, blocknr, nxt = struct.unpack_from(">III", block, at)
            out.append(Anode(clustersize, blocknr, nxt))
            if nxt == number:
                break
            number = nxt
        return out

    def _add_entry(self, dir_anode: int, entry: bytes) -> None:
        """Append a directory entry, extending the directory if it is full."""
        number = dir_anode
        while True:
            block_sector = self._read_anode(number).blocknr
            block = bytearray(self._read_reserved(block_sector))
            used = SIZEOF_DIRBLOCK_HEADER
            while used < self.reserved_blksize and block[used]:
                used += block[used]
            if used + len(entry) + 1 <= self.reserved_blksize:
                block[used:used + len(entry)] = entry
                self._write_sectors(block_sector, bytes(block))
                return
            nxt = self._read_anode(number).next
            if not nxt:
                break
            number = nxt

        #  Directory is full: chain another block onto it.
        new_sector = self.alloc_reserved()
        new_anode = self.alloc_anode()
        first = self._read_reserved(self._read_anode(dir_anode).blocknr)
        parent = struct.unpack_from(">I", first, 16)[0]
        self._write_dirblock(new_sector, dir_anode, parent=parent)
        self.set_anode(new_anode, 1, new_sector)
        current = self._read_anode(number)
        self.set_anode(number, current.clustersize, current.blocknr, new_anode)
        block = bytearray(self._read_reserved(new_sector))
        block[SIZEOF_DIRBLOCK_HEADER:SIZEOF_DIRBLOCK_HEADER + len(entry)] = entry
        self._write_sectors(new_sector, bytes(block))

    def _read_anode(self, anodenr: int) -> Anode:
        seqnr, offset = anodenr >> 16, anodenr & 0xFFFF
        block = self._read_reserved(self._anode_block_for(seqnr))
        at = SIZEOF_ANODEBLOCK_HEADER + offset * SIZEOF_ANODE
        clustersize, blocknr, nxt = struct.unpack_from(">III", block, at)
        return Anode(clustersize, blocknr, nxt)

    def find_entry(self, dir_anode: int, name: str) -> tuple[int, bool] | None:
        """Look for ``name`` in a directory, returning (anode, is_dir).

        Needed because filling a volume is not purely append-only: a later pass
        may add a file to a directory an earlier pass created, and creating a
        second directory of the same name would hide the first.
        """
        lowered = name.lower()
        for node in self._chain(dir_anode):
            for index in range(max(1, node.clustersize)):
                sector = node.blocknr + index * self.rescluster
                if not sector:
                    continue
                block = self._read_reserved(sector)
                if block[0:2] != ID_DIRBLOCK:
                    continue
                offset = SIZEOF_DIRBLOCK_HEADER
                while offset < self.reserved_blksize and block[offset]:
                    size = block[offset]
                    entry_type = struct.unpack_from(">b", block, offset + 1)[0]
                    anode = struct.unpack_from(">I", block, offset + 2)[0]
                    nlength = block[offset + 17]
                    entry_name = block[offset + 18:offset + 18 + nlength]
                    if entry_name.decode("latin-1").lower() == lowered:
                        return anode, entry_type >= 0
                    offset += size
        return None

    def _entry_exists(self, parent: int, name: str):
        return self.find_entry(parent, name)

    def mkdir(self, parent_anode: int, name: str, *, protect: int = 0,
              comment: str = "", days: int = 0, mins: int = 0,
              ticks: int = 0, check_existing: bool = True) -> int:
        protection = protect
        if check_existing:
            found = self.find_entry(parent_anode, name)
            if found is not None:
                if not found[1]:
                    raise Pfs3Error(f"{name} already exists as a file")
                return found[0]
        anodenr = self.alloc_anode()
        sector = self.alloc_reserved()
        self._write_dirblock(sector, anodenr, parent=parent_anode)
        self.set_anode(anodenr, 1, sector)
        self._add_entry(parent_anode,
                        self._direntry(name, anodenr, True, 0, protection,
                                       comment, days, mins, ticks))
        return anodenr

    def makedirs(self, path: str, parent: int | None = None) -> int:
        """Create a path, reusing directories that already exist."""
        current = self.root_anode if parent is None else parent
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            current = self.mkdir(current, part, check_existing=True)
        return current

    def write_file(self, parent_anode: int, name: str, data: bytes, *,
                   protect: int = 0, comment: str = "", days: int = 0,
                   mins: int = 0, ticks: int = 0,
                   check_existing: bool = True) -> int:
        """Write a file as a single extent - PFS3 anodes describe runs."""
        protection = protect
        if check_existing and self.find_entry(parent_anode, name) is not None:
            raise Pfs3Error(f"{name} already exists")
        anodenr = self.alloc_anode()
        blocks = (len(data) + SECTOR - 1) // SECTOR
        if blocks:
            start = self.alloc_data(blocks)
            padded = data + b"\0" * (blocks * SECTOR - len(data))
            self._write_sectors(start, padded)
            self.set_anode(anodenr, blocks, start)
        else:
            self.set_anode(anodenr, 0, 0)
        self._add_entry(parent_anode,
                        self._direntry(name, anodenr, False, len(data),
                                       protection, comment, days, mins, ticks))
        return anodenr


    # ------------------------------------------------- FFS-compatible facade

    @property
    def root(self) -> int:
        """Alias for the root directory's anode.

        :mod:`pistorm_imager.core.amigaos` fills FFS and PFS3 volumes with the
        same code, so both writers expose a ``root`` handle, ``mkdir``,
        ``write_file`` and ``makedirs`` with the same signatures.
        """
        return self.root_anode
