"""Amiga Rigid Disk Block (RDB) reading and writing.

The 0x76 MBR partition is handed to AmigaOS as a whole drive, so the Amiga's own
partition table - the RDB - lives at the *start of that partition*, not at the
start of the card.  All offsets in this module are therefore relative to the
0x76 partition unless stated otherwise.

Layout written by :func:`build`::

    block 0     RDSK   RigidDiskBlock
    block 1..n  PART   one per Amiga partition (DH0, DH1, ...)
    block ..    FSHD   optional file system header (e.g. PFS3)
    block ..    LSEG   file system executable, chained

Structure definitions follow ``devices/hardblocks.h`` from the Amiga includes.
"""
from __future__ import annotations

import dataclasses
import struct
from typing import BinaryIO, Iterable

BLOCK = 512
END = 0xFFFFFFFF

ID_RDSK = b"RDSK"
ID_PART = b"PART"
ID_FSHD = b"FSHD"
ID_LSEG = b"LSEG"
ID_BADB = b"BADB"

# pb_Flags
PBF_BOOTABLE = 1
PBF_NOMOUNT = 2

# Common DOS types
DOSTYPE_OFS = 0x444F5300      # DOS\0
DOSTYPE_FFS = 0x444F5301      # DOS\1
DOSTYPE_OFS_INTL = 0x444F5302  # DOS\2
DOSTYPE_FFS_INTL = 0x444F5303  # DOS\3 - the usual choice for a 3.x system disk
DOSTYPE_OFS_DC = 0x444F5304
DOSTYPE_FFS_DC = 0x444F5305
DOSTYPE_SFS0 = 0x53465300     # SFS\0
DOSTYPE_PFS3 = 0x50465303     # PFS\3
DOSTYPE_PDS3 = 0x50445303     # PDS\3 - PFS3 with direct SCSI

DOSTYPE_NAMES = {
    DOSTYPE_OFS: "OFS", DOSTYPE_FFS: "FFS",
    DOSTYPE_OFS_INTL: "OFS-INTL", DOSTYPE_FFS_INTL: "FFS-INTL",
    DOSTYPE_OFS_DC: "OFS-DC", DOSTYPE_FFS_DC: "FFS-DC",
    DOSTYPE_SFS0: "SFS", DOSTYPE_PFS3: "PFS3", DOSTYPE_PDS3: "PDS3",
}


def dostype_name(dostype: int) -> str:
    if dostype in DOSTYPE_NAMES:
        return DOSTYPE_NAMES[dostype]
    tag = bytes([(dostype >> 24) & 0xFF, (dostype >> 16) & 0xFF, (dostype >> 8) & 0xFF])
    printable = "".join(chr(b) if 32 <= b < 127 else "?" for b in tag)
    return f"{printable}\\{dostype & 0xFF}"


def parse_dostype(text: str) -> int:
    """Accept 'PFS3', 'DOS\\3', '0x444f5303' or 'PDS\\3'."""
    text = text.strip()
    upper = text.upper()
    for value, name in DOSTYPE_NAMES.items():
        if upper == name:
            return value
    if upper.startswith("0X"):
        return int(text, 16)
    if len(text) == 5 and text[3] == "\\":
        return (ord(text[0]) << 24) | (ord(text[1]) << 16) | (ord(text[2]) << 8) | int(text[4])
    raise ValueError(f"cannot parse DOS type {text!r}")


def _checksum(block: bytearray, summed_longs: int) -> None:
    """Set the checksum long (offset 8) so the block's longs sum to zero."""
    struct.pack_into(">i", block, 8, 0)
    total = 0
    for index in range(summed_longs):
        total = (total + struct.unpack_from(">I", block, index * 4)[0]) & 0xFFFFFFFF
    struct.pack_into(">I", block, 8, (-total) & 0xFFFFFFFF)


def _verify(block: bytes, summed_longs: int) -> bool:
    total = 0
    for index in range(summed_longs):
        total = (total + struct.unpack_from(">I", block, index * 4)[0]) & 0xFFFFFFFF
    return total == 0


def _bstr(name: str, field_len: int = 32) -> bytes:
    """Amiga BSTR: a length byte followed by the characters, zero padded."""
    raw = name.encode("latin-1", errors="replace")[: field_len - 2]
    return bytes([len(raw)]) + raw + b"\0" * (field_len - 1 - len(raw))


def _read_bstr(raw: bytes) -> str:
    length = min(raw[0], len(raw) - 1)
    return raw[1:1 + length].decode("latin-1")


@dataclasses.dataclass
class Geometry:
    """Synthetic drive geometry used by the RDB.

    The values do not have to match anything physical - the SD card has no real
    cylinders - but partitions must start and end on cylinder boundaries, so the
    cylinder size determines how finely partitions can be sized.  1 MiB per
    cylinder keeps the arithmetic obvious and the alignment SD-friendly.
    """

    heads: int = 16
    sectors: int = 128
    block_size: int = BLOCK

    @property
    def cyl_blocks(self) -> int:
        return self.heads * self.sectors

    @property
    def cyl_bytes(self) -> int:
        return self.cyl_blocks * self.block_size

    def cylinders_for(self, total_blocks: int) -> int:
        return total_blocks // self.cyl_blocks


@dataclasses.dataclass
class Partition:
    """One Amiga partition (a DHx) inside the RDB."""

    drive_name: str = "DH0"
    low_cyl: int = 0
    high_cyl: int = 0
    dostype: int = DOSTYPE_FFS_INTL
    bootable: bool = True
    boot_priority: int = 0
    automount: bool = True
    num_buffers: int = 300
    buf_mem_type: int = 0
    max_transfer: int = 0x0001FE00
    mask: int = 0x7FFFFFFE
    reserved_blocks: int = 2
    sectors_per_block: int = 1
    #  Filled in when the partition is read back from an existing disk.
    block_size: int = BLOCK

    def blocks(self, geom: Geometry) -> int:
        return (self.high_cyl - self.low_cyl + 1) * geom.cyl_blocks

    def size_bytes(self, geom: Geometry) -> int:
        return self.blocks(geom) * geom.block_size

    def start_block(self, geom: Geometry) -> int:
        return self.low_cyl * geom.cyl_blocks

    def byte_offset(self, geom: Geometry, base: int = 0) -> int:
        """Where this partition's data begins, in bytes from ``base``.

        ``base`` is where the RDB itself sits - the start of the 0x76 partition
        for a card, or zero for a bare hard disk image.
        """
        return base + self.start_block(geom) * BLOCK

    @property
    def dostype_name(self) -> str:
        return dostype_name(self.dostype)


@dataclasses.dataclass
class FileSystem:
    """A file system handler embedded in the RDB (FSHD + LSEG chain)."""

    dostype: int
    seglist: bytes
    version: int = 0
    #  PatchFlags 0x180 patches de_ fields 7 (Type) and 8 (Task)... the common
    #  value 0x180 sets Global Vector and Stack Size; 0x10 sets DosType only.
    patch_flags: int = 0x180
    stack_size: int = 0
    priority: int = 0
    global_vec: int = -1


@dataclasses.dataclass
class Rdb:
    geometry: Geometry = dataclasses.field(default_factory=Geometry)
    partitions: list[Partition] = dataclasses.field(default_factory=list)
    filesystems: list[FileSystem] = dataclasses.field(default_factory=list)
    cylinders: int = 0
    rdb_blocks_hi: int = 2015  # reserve the customary first 2016 blocks
    disk_vendor: str = "PiStorm"
    disk_product: str = "Emu68 SD"
    disk_revision: str = "1.0"

    # ------------------------------------------------------------- building

    def to_bytes(self) -> bytes:
        """Serialise the whole RDB area (block 0 .. rdb_blocks_hi)."""
        area = bytearray(BLOCK * (self.rdb_blocks_hi + 1))

        def put(block_no: int, data: bytes) -> None:
            area[block_no * BLOCK:(block_no + 1) * BLOCK] = data

        next_block = 1
        # --- partition blocks
        part_blocks: list[int] = []
        for _ in self.partitions:
            part_blocks.append(next_block)
            next_block += 1
        # --- file system header + segment blocks
        fs_blocks: list[int] = []
        fs_segs: list[list[int]] = []
        for fs in self.filesystems:
            fs_blocks.append(next_block)
            next_block += 1
            chunks = [fs.seglist[i:i + 492] for i in range(0, len(fs.seglist), 492)] or [b""]
            segs = list(range(next_block, next_block + len(chunks)))
            next_block += len(chunks)
            fs_segs.append(segs)
        if next_block > self.rdb_blocks_hi + 1:
            raise ValueError("RDB metadata does not fit in the reserved area")

        put(0, self._rigid_block(part_blocks[0] if part_blocks else END,
                                 fs_blocks[0] if fs_blocks else END))
        for index, (part, block_no) in enumerate(zip(self.partitions, part_blocks)):
            nxt = part_blocks[index + 1] if index + 1 < len(part_blocks) else END
            put(block_no, self._partition_block(part, nxt))
        for index, (fs, block_no) in enumerate(zip(self.filesystems, fs_blocks)):
            nxt = fs_blocks[index + 1] if index + 1 < len(fs_blocks) else END
            segs = fs_segs[index]
            put(block_no, self._fshd_block(fs, nxt, segs[0] if segs else END, len(segs)))
            chunks = [fs.seglist[i:i + 492] for i in range(0, len(fs.seglist), 492)] or [b""]
            for seg_index, (seg_block, chunk) in enumerate(zip(segs, chunks)):
                seg_next = segs[seg_index + 1] if seg_index + 1 < len(segs) else END
                put(seg_block, self._lseg_block(chunk, seg_next))
        return bytes(area)

    def _rigid_block(self, part_list: int, fs_list: int) -> bytes:
        geom = self.geometry
        block = bytearray(BLOCK)
        struct.pack_into(">4sIiII", block, 0, ID_RDSK, 64, 0, 7, geom.block_size)
        struct.pack_into(">I", block, 20, 0x00000017)  # flags: last lun/tid/no reselect
        struct.pack_into(">IIII", block, 24, END, part_list, fs_list, END)
        struct.pack_into(">6I", block, 40, *([END] * 6))
        struct.pack_into(">IIII", block, 64, self.cylinders, geom.sectors, geom.heads, 1)
        struct.pack_into(">I", block, 80, self.cylinders)          # park
        struct.pack_into(">3I", block, 84, *([END] * 3))
        struct.pack_into(">III", block, 96, self.cylinders, self.cylinders, 3)
        struct.pack_into(">5I", block, 108, *([END] * 5))
        struct.pack_into(">IIII", block, 128, 0, self.rdb_blocks_hi, 0, self.cylinders - 1)
        struct.pack_into(">III", block, 144, geom.cyl_blocks, 0, self.rdb_blocks_hi)
        struct.pack_into(">I", block, 156, END)
        block[160:168] = self.disk_vendor.encode("latin-1")[:8].ljust(8, b"\0")
        block[168:184] = self.disk_product.encode("latin-1")[:16].ljust(16, b"\0")
        block[184:188] = self.disk_revision.encode("latin-1")[:4].ljust(4, b"\0")
        _checksum(block, 64)
        return bytes(block)

    def _partition_block(self, part: Partition, next_block: int) -> bytes:
        geom = self.geometry
        block = bytearray(BLOCK)
        flags = 0
        if part.bootable:
            flags |= PBF_BOOTABLE
        if not part.automount:
            flags |= PBF_NOMOUNT
        struct.pack_into(">4sIiIII", block, 0, ID_PART, 64, 0, 7, next_block, flags)
        struct.pack_into(">2I", block, 24, 0, 0)
        struct.pack_into(">I", block, 32, 0)  # pb_DevFlags
        block[36:68] = _bstr(part.drive_name, 32)
        struct.pack_into(">15I", block, 68, *([0] * 15))
        env = [
            16,                              # de_TableSize
            geom.block_size // 4,            # de_SizeBlock (longs)
            0,                               # de_SecOrg
            geom.heads,                      # de_Surfaces
            part.sectors_per_block,          # de_SectorPerBlock
            geom.sectors,                    # de_BlocksPerTrack
            part.reserved_blocks,            # de_Reserved
            0,                               # de_PreAlloc
            0,                               # de_Interleave
            part.low_cyl,                    # de_LowCyl
            part.high_cyl,                   # de_HighCyl
            part.num_buffers,                # de_NumBuffers
            part.buf_mem_type,               # de_BufMemType
            part.max_transfer,               # de_MaxTransfer
            part.mask,                       # de_Mask
            part.boot_priority & 0xFFFFFFFF,  # de_BootPri
            part.dostype,                    # de_DosType
        ]
        struct.pack_into(">17I", block, 128, *env)
        _checksum(block, 64)
        return bytes(block)

    def _fshd_block(self, fs: FileSystem, next_block: int, seg_block: int,
                    seg_count: int = 0) -> bytes:
        block = bytearray(BLOCK)
        struct.pack_into(">4sIiIII", block, 0, ID_FSHD, 64, 0, 7, next_block, 0)
        struct.pack_into(">2I", block, 24, 0, 0)
        struct.pack_into(">IIII", block, 32, fs.dostype, fs.version, fs.patch_flags, 0)
        struct.pack_into(">III", block, 48, 0, 0, 0)          # Task, Lock, Handler
        struct.pack_into(">I", block, 60, fs.stack_size)
        struct.pack_into(">i", block, 64, fs.priority)
        struct.pack_into(">i", block, 68, 0)          # fhb_Startup
        #  fhb_SegListBlocks holds the *block number* of the first LoadSegBlock,
        #  not a count.  Real Amiga RDBs (checked against an HstWB image) put the
        #  pointer here, and AmigaOS will not find the handler anywhere else.
        struct.pack_into(">i", block, 72, seg_block if seg_block != END else -1)
        struct.pack_into(">i", block, 76, fs.global_vec)
        _checksum(block, 64)
        return bytes(block)

    def _lseg_block(self, chunk: bytes, next_block: int) -> bytes:
        block = bytearray(BLOCK)
        struct.pack_into(">4sIiII", block, 0, ID_LSEG, 128, 0, 7, next_block)
        block[20:20 + len(chunk)] = chunk
        _checksum(block, 128)
        return bytes(block)

    # -------------------------------------------------------------- reading

    @classmethod
    def read(cls, handle: BinaryIO, base_offset: int = 0) -> "Rdb":
        """Parse an existing RDB whose block 0 lives at ``base_offset`` bytes."""
        for probe in range(16):
            handle.seek(base_offset + probe * BLOCK)
            block = handle.read(BLOCK)
            if len(block) == BLOCK and block[0:4] == ID_RDSK:
                break
        else:
            raise ValueError("no RigidDiskBlock found in the first 16 blocks")
        if not _verify(block, struct.unpack_from(">I", block, 4)[0]):
            raise ValueError("RigidDiskBlock checksum is bad")

        block_size = struct.unpack_from(">I", block, 16)[0] or BLOCK
        part_list, fs_list = struct.unpack_from(">II", block, 28)
        cylinders, sectors, heads = struct.unpack_from(">III", block, 64)
        rdb_hi = struct.unpack_from(">I", block, 132)[0]
        rdb = cls(
            geometry=Geometry(heads=heads, sectors=sectors, block_size=block_size),
            cylinders=cylinders,
            rdb_blocks_hi=rdb_hi,
            disk_vendor=block[160:168].decode("latin-1").strip("\0 "),
            disk_product=block[168:184].decode("latin-1").strip("\0 "),
            disk_revision=block[184:188].decode("latin-1").strip("\0 "),
        )

        seen: set[int] = set()
        cursor = part_list
        while cursor != END and cursor not in seen and cursor != 0xFFFFFFFF:
            seen.add(cursor)
            handle.seek(base_offset + cursor * block_size)
            pb = handle.read(BLOCK)
            if len(pb) < BLOCK or pb[0:4] != ID_PART:
                break
            flags = struct.unpack_from(">I", pb, 20)[0]
            env = struct.unpack_from(">17I", pb, 128)
            boot_pri = struct.unpack_from(">i", pb, 128 + 15 * 4)[0]
            rdb.partitions.append(Partition(
                drive_name=_read_bstr(pb[36:68]),
                low_cyl=env[9], high_cyl=env[10], dostype=env[16],
                bootable=bool(flags & PBF_BOOTABLE),
                automount=not (flags & PBF_NOMOUNT),
                boot_priority=boot_pri,
                num_buffers=env[11], buf_mem_type=env[12],
                max_transfer=env[13], mask=env[14],
                reserved_blocks=env[6], sectors_per_block=env[4],
                block_size=block_size,
            ))
            cursor = struct.unpack_from(">I", pb, 16)[0]

        seen.clear()
        cursor = fs_list
        while cursor != END and cursor not in seen:
            seen.add(cursor)
            handle.seek(base_offset + cursor * block_size)
            fb = handle.read(BLOCK)
            if len(fb) < BLOCK or fb[0:4] != ID_FSHD:
                break
            dostype, version, patch_flags = struct.unpack_from(">III", fb, 32)
            seg_start = struct.unpack_from(">i", fb, 72)[0]
            payload = bytearray()
            seg = seg_start
            guard = 0
            while seg not in (-1, END) and guard < 100000:
                guard += 1
                handle.seek(base_offset + seg * block_size)
                sb = handle.read(BLOCK)
                if len(sb) < BLOCK or sb[0:4] != ID_LSEG:
                    break
                payload += sb[20:512]
                seg = struct.unpack_from(">i", sb, 16)[0]
                if seg == -1:
                    break
            rdb.filesystems.append(FileSystem(
                dostype=dostype, seglist=bytes(payload), version=version,
                patch_flags=patch_flags,
                stack_size=struct.unpack_from(">I", fb, 60)[0],
                priority=struct.unpack_from(">i", fb, 64)[0],
                global_vec=struct.unpack_from(">i", fb, 76)[0],
            ))
            cursor = struct.unpack_from(">I", fb, 16)[0]
        return rdb

    # ------------------------------------------------------------ utilities

    def describe(self) -> str:
        from .util import human_size
        geom = self.geometry
        lines = [f"Geometry: {self.cylinders} cyl x {geom.heads} heads x "
                 f"{geom.sectors} sec ({human_size(geom.cyl_bytes)}/cyl)"]
        for part in self.partitions:
            flags = []
            if part.bootable:
                flags.append(f"bootable pri {part.boot_priority}")
            if not part.automount:
                flags.append("no automount")
            lines.append(
                f"  {part.drive_name:<8} {part.dostype_name:<9} "
                f"cyl {part.low_cyl}-{part.high_cyl} "
                f"{human_size(part.size_bytes(geom)):>10}"
                + (f"  [{', '.join(flags)}]" if flags else "")
            )
        for fs in self.filesystems:
            lines.append(f"  filesystem {dostype_name(fs.dostype)} "
                         f"version {fs.version >> 16}.{fs.version & 0xFFFF} "
                         f"({len(fs.seglist)} bytes)")
        return "\n".join(lines)

    def write(self, handle: BinaryIO, base_offset: int = 0) -> None:
        handle.seek(base_offset)
        handle.write(self.to_bytes())


def layout(geom: Geometry, total_blocks: int, specs: Iterable[tuple[str, int | None, int]],
           *, reserved_blocks: int = 2016) -> list[Partition]:
    """Turn ``(name, size_bytes_or_None, dostype)`` into cylinder-aligned parts.

    A size of ``None`` means "use whatever is left", which is how the Work
    partition normally soaks up the rest of the card.
    """
    specs = list(specs)
    first_cyl = max(1, -(-reserved_blocks // geom.cyl_blocks))
    total_cyls = total_blocks // geom.cyl_blocks
    fixed = 0
    for _, size, _dt in specs:
        if size is not None:
            fixed += -(-size // geom.cyl_bytes)
    available = total_cyls - first_cyl
    if fixed > available:
        raise ValueError("requested partitions do not fit in the available space")
    flexible = [i for i, (_, size, _dt) in enumerate(specs) if size is None]
    share = (available - fixed) // len(flexible) if flexible else 0

    parts: list[Partition] = []
    cursor = first_cyl
    for index, (name, size, dostype) in enumerate(specs):
        cyls = -(-size // geom.cyl_bytes) if size is not None else share
        if index == len(specs) - 1:
            cyls = total_cyls - cursor  # give any rounding remainder to the last
        if cyls <= 0:
            raise ValueError(f"partition {name} would be empty")
        parts.append(Partition(
            drive_name=name,
            low_cyl=cursor,
            high_cyl=cursor + cyls - 1,
            dostype=dostype,
            bootable=(index == 0),
            boot_priority=0 if index == 0 else -128,
        ))
        cursor += cyls
    return parts
