"""Reading and writing Amiga OFS/FFS volumes.

The same on-disk format covers a floppy image (an ``.adf``) and a hard disk
partition, so one implementation serves both: we read Workbench files out of
ADFs and write them into an FFS partition on the card.

Block layout follows the Amiga Disk Format specification.  Every structure is
big-endian, 512 bytes, and carries a checksum long that must make the block's
longs sum to zero.

Only what an installer needs is implemented: reading whole volumes, and writing
a fresh volume from scratch.  There is no support for deleting or rewriting
files in place, because an install only ever adds.
"""
from __future__ import annotations

import dataclasses
import struct
from typing import BinaryIO, Iterator

BLOCK = 512
LONGS = BLOCK // 4          # 128
HT_SIZE = LONGS - 56        # 72 hash table / data pointer entries
MAX_NAME = 30
MAX_COMMENT = 79

# Block primary types
T_HEADER = 2
T_DATA = 8
T_LIST = 16

# Block secondary types
ST_ROOT = 1
ST_USERDIR = 2
ST_FILE = -3
ST_LINKFILE = -4
ST_LINKDIR = 4
ST_SOFTLINK = 3

# Boot block flags
FLAG_FFS = 1
FLAG_INTL = 2
FLAG_DIRCACHE = 4

OFS_DATA_SIZE = BLOCK - 24   # 488 payload bytes per OFS data block

DOSTYPE_BASE = 0x444F5300    # 'DOS\0'


class AmigaFsError(RuntimeError):
    pass


def dostype_flags(dostype: int) -> int:
    return dostype & 0xFF


def is_dos_family(dostype: int) -> bool:
    """Whether the ROM's own file system handles this, rather than a handler.

    Only the low byte of a dostype carries the OFS/FFS flags, so testing that
    byte alone says nothing about which file system it belongs to: PFS3 is
    'PFS\\3', whose flag byte reads as FFS-with-directory-cache.
    """
    return dostype & 0xFFFFFF00 == DOSTYPE_BASE


def is_ffs(dostype: int) -> bool:
    return bool(dostype_flags(dostype) & FLAG_FFS)


def is_intl(dostype: int) -> bool:
    """International mode: also used implicitly by directory-cache volumes."""
    flags = dostype_flags(dostype)
    return bool(flags & FLAG_INTL) or bool(flags & FLAG_DIRCACHE)


def checksum(block: bytes | bytearray, offset: int = 20) -> int:
    """Value for the checksum long so the block's longs sum to zero."""
    total = 0
    for index in range(LONGS):
        if index * 4 == offset:
            continue
        total = (total + struct.unpack_from(">I", block, index * 4)[0]) & 0xFFFFFFFF
    return (-total) & 0xFFFFFFFF


def apply_checksum(block: bytearray, offset: int = 20) -> None:
    struct.pack_into(">I", block, offset, 0)
    struct.pack_into(">I", block, offset, checksum(block, offset))


def verify_checksum(block: bytes, offset: int = 20) -> bool:
    total = 0
    for index in range(LONGS):
        total = (total + struct.unpack_from(">I", block, index * 4)[0]) & 0xFFFFFFFF
    return total == 0


def _upper(code: int, intl: bool) -> int:
    if intl:
        if 97 <= code <= 122 or 224 <= code <= 254 and code != 247:
            return code - 32
        return code
    return code - 32 if 97 <= code <= 122 else code


def hash_name(name: str, intl: bool) -> int:
    """AmigaDOS directory hash. Case- and (in intl mode) accent-insensitive."""
    raw = name.encode("latin-1", errors="replace")
    value = len(raw)
    for byte in raw:
        value = (value * 13 + _upper(byte, intl)) & 0x7FF
    return value % HT_SIZE


def names_equal(left: str, right: str, intl: bool) -> bool:
    if len(left) != len(right):
        return False
    a = left.encode("latin-1", errors="replace")
    b = right.encode("latin-1", errors="replace")
    return all(_upper(x, intl) == _upper(y, intl) for x, y in zip(a, b))


def read_bstr(block: bytes, offset: int, limit: int = MAX_NAME) -> str:
    length = min(block[offset], limit)
    return block[offset + 1:offset + 1 + length].decode("latin-1")


def write_bstr(block: bytearray, offset: int, text: str, limit: int = MAX_NAME) -> None:
    raw = text.encode("latin-1", errors="replace")[:limit]
    block[offset] = len(raw)
    block[offset + 1:offset + 1 + len(raw)] = raw


def root_block_number(total_blocks: int, reserved: int = 2) -> int:
    """Where AmigaDOS puts the root block of a volume of this size."""
    return (total_blocks - 1 + reserved) // 2


@dataclasses.dataclass
class Entry:
    """One directory entry inside an Amiga volume."""

    name: str
    block: int
    is_dir: bool
    size: int = 0
    protect: int = 0
    comment: str = ""
    days: int = 0
    mins: int = 0
    ticks: int = 0

    @property
    def is_file(self) -> bool:
        return not self.is_dir


class Volume:
    """An OFS/FFS volume living inside ``handle`` at ``offset``."""

    def __init__(self, handle: BinaryIO, offset: int = 0,
                 total_blocks: int | None = None, reserved: int = 2):
        self.f = handle
        self.base = offset
        self.reserved = reserved
        if total_blocks is None:
            handle.seek(0, 2)
            total_blocks = (handle.tell() - offset) // BLOCK
        self.total_blocks = total_blocks
        self._read_boot()

    # ------------------------------------------------------------ low level

    def read_block(self, number: int) -> bytes:
        if not 0 <= number < self.total_blocks:
            raise AmigaFsError(f"block {number} is outside the volume")
        self.f.seek(self.base + number * BLOCK)
        data = self.f.read(BLOCK)
        if len(data) != BLOCK:
            raise AmigaFsError(f"short read at block {number}")
        return data

    def write_block(self, number: int, data: bytes) -> None:
        if len(data) != BLOCK:
            raise AmigaFsError("blocks must be exactly 512 bytes")
        if not 0 <= number < self.total_blocks:
            raise AmigaFsError(f"block {number} is outside the volume")
        self.f.seek(self.base + number * BLOCK)
        self.f.write(data)

    def _read_boot(self) -> None:
        boot = self.read_block(0)
        if boot[0:3] != b"DOS":
            raise AmigaFsError("not an Amiga file system (no 'DOS' signature)")
        self.dostype = struct.unpack_from(">I", boot, 0)[0]
        self.ffs = is_ffs(self.dostype)
        self.intl = is_intl(self.dostype)
        self.root = root_block_number(self.total_blocks, self.reserved)

    # -------------------------------------------------------------- reading

    @property
    def name(self) -> str:
        return read_bstr(self.read_block(self.root), 432)

    def _hash_table(self, block_number: int) -> list[int]:
        block = self.read_block(block_number)
        return list(struct.unpack_from(f">{HT_SIZE}I", block, 24))

    def _entry_from_block(self, number: int) -> Entry | None:
        block = self.read_block(number)
        if struct.unpack_from(">I", block, 0)[0] != T_HEADER:
            return None
        sec_type = struct.unpack_from(">i", block, 508)[0]
        if sec_type not in (ST_USERDIR, ST_FILE, ST_LINKDIR, ST_LINKFILE, ST_SOFTLINK):
            return None
        return Entry(
            name=read_bstr(block, 432),
            block=number,
            is_dir=sec_type in (ST_USERDIR, ST_LINKDIR),
            size=struct.unpack_from(">I", block, 324)[0] if sec_type == ST_FILE else 0,
            protect=struct.unpack_from(">I", block, 320)[0],
            comment=read_bstr(block, 328, MAX_COMMENT),
            days=struct.unpack_from(">I", block, 420)[0],
            mins=struct.unpack_from(">I", block, 424)[0],
            ticks=struct.unpack_from(">I", block, 428)[0],
        )

    def listdir(self, block_number: int | None = None) -> list[Entry]:
        """Entries of a directory, following every hash chain."""
        block_number = self.root if block_number is None else block_number
        entries: list[Entry] = []
        seen: set[int] = set()
        for head in self._hash_table(block_number):
            current = head
            while current:
                if current in seen:
                    break                      # damaged image: stop, don't loop
                seen.add(current)
                entry = self._entry_from_block(current)
                if entry is None:
                    break
                entries.append(entry)
                current = struct.unpack_from(">I", self.read_block(current), 496)[0]
        return sorted(entries, key=lambda e: e.name.lower())

    def walk(self, block_number: int | None = None,
             path: str = "") -> Iterator[tuple[str, Entry]]:
        """Yield ``(path, entry)`` for everything under a directory."""
        for entry in self.listdir(block_number):
            full = f"{path}/{entry.name}" if path else entry.name
            yield full, entry
            if entry.is_dir:
                yield from self.walk(entry.block, full)

    def read_file(self, entry: Entry | int) -> bytes:
        """Read a file's contents, following its extension block chain."""
        block_number = entry.block if isinstance(entry, Entry) else entry
        header = self.read_block(block_number)
        size = struct.unpack_from(">I", header, 324)[0]
        out = bytearray()
        current = block_number
        while current:
            block = self.read_block(current)
            high_seq = struct.unpack_from(">I", block, 8)[0]
            pointers = struct.unpack_from(f">{HT_SIZE}I", block, 24)
            #  Data pointers are stored in reverse order.
            for index in range(high_seq):
                data_block = pointers[HT_SIZE - 1 - index]
                if not data_block:
                    continue
                raw = self.read_block(data_block)
                if self.ffs:
                    out += raw
                else:
                    used = struct.unpack_from(">I", raw, 12)[0]
                    out += raw[24:24 + min(used, OFS_DATA_SIZE)]
                if len(out) >= size:
                    break
            current = struct.unpack_from(">I", block, 504)[0]  # extension block
            if len(out) >= size:
                break
        if len(out) < size:
            raise AmigaFsError(
                f"file is truncated: expected {size} bytes, recovered {len(out)}")
        return bytes(out[:size])

    def find(self, path: str) -> Entry | None:
        """Look up a path such as ``Libs/workbench.library``."""
        current = self.root
        entry: Entry | None = None
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            entry = next((e for e in self.listdir(current)
                          if names_equal(e.name, part, self.intl)), None)
            if entry is None:
                return None
            current = entry.block
        return entry

    def describe(self) -> str:
        kind = "FFS" if self.ffs else "OFS"
        if self.intl:
            kind += "-INTL"
        return (f'"{self.name}" ({kind}, {self.total_blocks} blocks, '
                f'root at {self.root})')


# ---------------------------------------------------------------- writing

DOSTYPE_OFS = 0x444F5300
DOSTYPE_FFS = 0x444F5301
DOSTYPE_OFS_INTL = 0x444F5302
DOSTYPE_FFS_INTL = 0x444F5303

BITMAP_LONGS = LONGS - 1          # a bitmap block holds 127 longs of bits
BITS_PER_BITMAP = BITMAP_LONGS * 32   # 4064 blocks described per bitmap block
ROOT_BM_PAGES = 25                # bitmap pointers that fit in the root block
BM_EXT_POINTERS = LONGS - 1       # pointers in a bitmap extension block

#  FFS with 512-byte blocks is fine well past this, but AmigaOS 3.1's own
#  file system starts misbehaving on very large volumes, and a Workbench
#  partition never needs to be huge.
FFS_SAFE_LIMIT = 4 * 1024 * 1024 * 1024


class VolumeWriter(Volume):
    """Creates and populates an OFS/FFS volume.

    The bitmap is held in memory (one byte per block) while writing and packed
    out at :meth:`close`, which keeps allocation simple and avoids rewriting
    bitmap blocks for every data block.
    """

    def __init__(self, handle: BinaryIO, offset: int, total_blocks: int,
                 reserved: int = 2, *, _skip_boot: bool = False):
        self.f = handle
        self.base = offset
        self.reserved = reserved
        self.total_blocks = total_blocks
        if not _skip_boot:
            self._read_boot()
        self._free = bytearray()
        self._rover = 0
        self._bitmap_blocks: list[int] = []
        self._bitmap_ext_blocks: list[int] = []

    # -------------------------------------------------------- construction

    @classmethod
    def format(cls, handle: BinaryIO, offset: int, total_blocks: int, name: str,
               dostype: int = DOSTYPE_FFS_INTL, reserved: int = 2,
               *, bootable: bool = False) -> "VolumeWriter":
        """Lay down a brand new, empty volume."""
        if total_blocks < 32:
            raise AmigaFsError("volume is too small to hold a file system")
        if not is_ffs(dostype):
            raise AmigaFsError(
                "only FFS volumes can be created; OFS is read-only here")
        volume = cls(handle, offset, total_blocks, reserved, _skip_boot=True)
        volume.dostype = dostype
        volume.ffs = is_ffs(dostype)
        volume.intl = is_intl(dostype)
        volume.root = root_block_number(total_blocks, reserved)

        #  Everything free to begin with, then reserve the fixed structures.
        volume._free = bytearray(b"\1" * total_blocks)
        for block in range(reserved):
            volume._free[block] = 0
        volume._free[volume.root] = 0

        bits = total_blocks - reserved
        count = -(-bits // BITS_PER_BITMAP)
        extra = max(0, count - ROOT_BM_PAGES)
        ext_count = -(-extra // BM_EXT_POINTERS) if extra else 0
        #  Bitmap blocks sit immediately after the root block, as AmigaDOS does.
        cursor = volume.root + 1
        for _ in range(count):
            volume._bitmap_blocks.append(cursor)
            volume._free[cursor] = 0
            cursor += 1
        for _ in range(ext_count):
            volume._bitmap_ext_blocks.append(cursor)
            volume._free[cursor] = 0
            cursor += 1
        volume._rover = cursor

        #  Boot block: the DOS type, and optionally room for boot code.
        boot = bytearray(reserved * BLOCK)
        struct.pack_into(">I", boot, 0, dostype)
        handle.seek(offset)
        handle.write(bytes(boot))

        #  An empty root block.
        root = bytearray(BLOCK)
        struct.pack_into(">IIII", root, 0, T_HEADER, 0, 0, HT_SIZE)
        struct.pack_into(">i", root, 312, -1)          # bitmap is valid
        write_bstr(root, 432, name)
        struct.pack_into(">i", root, 508, ST_ROOT)
        volume.write_block(volume.root, bytes(root))
        volume._write_bitmap_pointers()
        volume.touch_root()
        return volume

    # ---------------------------------------------------------- allocation

    def allocate(self, count: int = 1) -> list[int]:
        """Reserve ``count`` blocks, preferring a contiguous run."""
        out: list[int] = []
        scanned = 0
        block = self._rover
        while len(out) < count:
            if scanned > self.total_blocks:
                raise AmigaFsError("the Amiga partition is full")
            if block >= self.total_blocks:
                block = self.reserved
                scanned += 1
                continue
            if self._free[block]:
                self._free[block] = 0
                out.append(block)
            block += 1
            scanned += 1
        self._rover = block
        return out

    @property
    def free_blocks(self) -> int:
        return sum(self._free)

    @property
    def free_bytes(self) -> int:
        return self.free_blocks * BLOCK

    @property
    def max_name_length(self) -> int:
        """FFS has a hard 30-character limit; PFS3 does not."""
        return MAX_NAME

    def _write_bitmap_pointers(self) -> None:
        root = bytearray(self.read_block(self.root))
        in_root = self._bitmap_blocks[:ROOT_BM_PAGES]
        for index in range(ROOT_BM_PAGES):
            value = in_root[index] if index < len(in_root) else 0
            struct.pack_into(">I", root, 316 + index * 4, value)
        struct.pack_into(">I", root, 416,
                         self._bitmap_ext_blocks[0] if self._bitmap_ext_blocks else 0)
        apply_checksum(root)
        self.write_block(self.root, bytes(root))

        remaining = self._bitmap_blocks[ROOT_BM_PAGES:]
        for position, ext_block in enumerate(self._bitmap_ext_blocks):
            chunk = remaining[position * BM_EXT_POINTERS:
                              (position + 1) * BM_EXT_POINTERS]
            block = bytearray(BLOCK)
            for index, pointer in enumerate(chunk):
                struct.pack_into(">I", block, index * 4, pointer)
            nxt = (self._bitmap_ext_blocks[position + 1]
                   if position + 1 < len(self._bitmap_ext_blocks) else 0)
            struct.pack_into(">I", block, BLOCK - 4, nxt)
            self.write_block(ext_block, bytes(block))

    def flush_bitmap(self) -> None:
        """Pack the in-memory free map into the volume's bitmap blocks."""
        for index, block_number in enumerate(self._bitmap_blocks):
            block = bytearray(BLOCK)
            first_bit = index * BITS_PER_BITMAP
            for long_index in range(BITMAP_LONGS):
                value = 0
                base = first_bit + long_index * 32
                for bit in range(32):
                    block_id = self.reserved + base + bit
                    if block_id < self.total_blocks and self._free[block_id]:
                        value |= 1 << bit
                struct.pack_into(">I", block, 4 + long_index * 4, value)
            apply_checksum(block, offset=0)
            self.write_block(block_number, bytes(block))

    # ------------------------------------------------------- entry writing

    def touch_root(self, days: int = 0, mins: int = 0, ticks: int = 0) -> None:
        root = bytearray(self.read_block(self.root))
        for offset in (420, 472, 484):
            struct.pack_into(">III", root, offset, days, mins, ticks)
        apply_checksum(root)
        self.write_block(self.root, bytes(root))

    def _link_into_directory(self, parent: int, name: str, child: int) -> None:
        """Add ``child`` to ``parent``'s hash table, at the head of its chain."""
        slot = hash_name(name, self.intl)
        parent_block = bytearray(self.read_block(parent))
        head = struct.unpack_from(">I", parent_block, 24 + slot * 4)[0]

        child_block = bytearray(self.read_block(child))
        struct.pack_into(">I", child_block, 496, head)      # hash_chain
        struct.pack_into(">I", child_block, 500, parent)    # parent
        apply_checksum(child_block)
        self.write_block(child, bytes(child_block))

        struct.pack_into(">I", parent_block, 24 + slot * 4, child)
        apply_checksum(parent_block)
        self.write_block(parent, bytes(parent_block))

    def _entry_exists(self, parent: int, name: str) -> Entry | None:
        return next((e for e in self.listdir(parent)
                     if names_equal(e.name, name, self.intl)), None)

    def mkdir(self, parent: int, name: str, *, protect: int = 0,
              comment: str = "", days: int = 0, mins: int = 0,
              ticks: int = 0, check_existing: bool = True) -> int:
        existing = self._entry_exists(parent, name) if check_existing else None
        if existing is not None:
            if not existing.is_dir:
                raise AmigaFsError(f"{name} already exists as a file")
            return existing.block
        block_number = self.allocate()[0]
        block = bytearray(BLOCK)
        struct.pack_into(">II", block, 0, T_HEADER, block_number)
        struct.pack_into(">I", block, 320, protect)
        write_bstr(block, 328, comment, MAX_COMMENT)
        struct.pack_into(">III", block, 420, days, mins, ticks)
        write_bstr(block, 432, name)
        struct.pack_into(">i", block, 508, ST_USERDIR)
        apply_checksum(block)
        self.write_block(block_number, bytes(block))
        self._link_into_directory(parent, name, block_number)
        return block_number

    def makedirs(self, path: str, parent: int | None = None) -> int:
        current = self.root if parent is None else parent
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            current = self.mkdir(current, part)
        return current

    def write_file(self, parent: int, name: str, data: bytes, *,
                   protect: int = 0, comment: str = "", days: int = 0,
                   mins: int = 0, ticks: int = 0,
                   check_existing: bool = True) -> int:
        """Write a file into ``parent``. FFS data blocks are raw 512-byte blocks.

        ``check_existing`` costs a full directory walk, which is O(n) per file
        and so O(n^2) over a directory.  Filling a fresh volume, where nothing
        can already be there, should pass ``False``.
        """
        if not self.ffs:
            raise AmigaFsError("writing to an OFS volume is not supported")
        if check_existing and self._entry_exists(parent, name) is not None:
            raise AmigaFsError(f"{name} already exists")

        chunks = [data[i:i + BLOCK] for i in range(0, len(data), BLOCK)] or []
        header_number = self.allocate()[0]
        data_blocks = self.allocate(len(chunks)) if chunks else []
        for number, chunk in zip(data_blocks, chunks):
            self.write_block(number, chunk.ljust(BLOCK, b"\0"))

        #  The header holds the first 72 pointers; the rest go in a chain of
        #  extension blocks, all storing their pointers in reverse order.
        groups = [data_blocks[i:i + HT_SIZE]
                  for i in range(0, len(data_blocks), HT_SIZE)] or [[]]
        ext_numbers = self.allocate(len(groups) - 1) if len(groups) > 1 else []

        header = bytearray(BLOCK)
        struct.pack_into(">IIII", header, 0, T_HEADER, header_number,
                         len(groups[0]), 0)
        struct.pack_into(">I", header, 16, data_blocks[0] if data_blocks else 0)
        for index, block_number in enumerate(groups[0]):
            struct.pack_into(">I", header, 24 + (HT_SIZE - 1 - index) * 4, block_number)
        struct.pack_into(">I", header, 320, protect)
        struct.pack_into(">I", header, 324, len(data))
        write_bstr(header, 328, comment, MAX_COMMENT)
        struct.pack_into(">III", header, 420, days, mins, ticks)
        write_bstr(header, 432, name)
        struct.pack_into(">I", header, 504, ext_numbers[0] if ext_numbers else 0)
        struct.pack_into(">i", header, 508, ST_FILE)
        apply_checksum(header)
        self.write_block(header_number, bytes(header))

        for position, ext_number in enumerate(ext_numbers):
            group = groups[position + 1]
            block = bytearray(BLOCK)
            struct.pack_into(">III", block, 0, T_LIST, ext_number, len(group))
            for index, block_number in enumerate(group):
                struct.pack_into(">I", block, 24 + (HT_SIZE - 1 - index) * 4,
                                 block_number)
            struct.pack_into(">I", block, 500, header_number)
            nxt = ext_numbers[position + 1] if position + 1 < len(ext_numbers) else 0
            struct.pack_into(">I", block, 504, nxt)
            struct.pack_into(">i", block, 508, ST_FILE)
            apply_checksum(block)
            self.write_block(ext_number, bytes(block))

        self._link_into_directory(parent, name, header_number)
        return header_number

    def close(self) -> None:
        self.flush_bitmap()
        self.f.flush()
