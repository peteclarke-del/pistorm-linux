"""Reading and writing the DOS/MBR partition table.

Emu68 requires an MBR (not GPT) with a FAT32 boot partition and one or more
partitions of type 0x76, which it exposes to AmigaOS as virtual hard drives.
"""
from __future__ import annotations

import dataclasses
import struct
from typing import BinaryIO

SECTOR = 512
SIGNATURE_OFFSET = 510
TABLE_OFFSET = 446

TYPE_EMPTY = 0x00
TYPE_FAT32_CHS = 0x0B
TYPE_FAT32_LBA = 0x0C
TYPE_AMIGA = 0x76  # "Amiga drive" - what Emu68 mounts as a hard disk

TYPE_NAMES = {
    TYPE_EMPTY: "empty",
    0x01: "FAT12",
    0x04: "FAT16 <32M",
    0x05: "extended",
    0x06: "FAT16",
    0x07: "NTFS/exFAT",
    TYPE_FAT32_CHS: "FAT32",
    TYPE_FAT32_LBA: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)",
    0x82: "Linux swap",
    0x83: "Linux",
    TYPE_AMIGA: "Amiga (0x76)",
}


@dataclasses.dataclass
class MbrPartition:
    index: int
    boot_flag: int
    type_id: int
    start_lba: int
    sector_count: int

    @property
    def empty(self) -> bool:
        return self.type_id == TYPE_EMPTY or self.sector_count == 0

    @property
    def start_bytes(self) -> int:
        return self.start_lba * SECTOR

    @property
    def size_bytes(self) -> int:
        return self.sector_count * SECTOR

    @property
    def end_lba(self) -> int:
        """First LBA past the end of this partition."""
        return self.start_lba + self.sector_count

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type_id, f"type 0x{self.type_id:02x}")

    def __str__(self) -> str:
        from .util import human_size
        return (f"#{self.index + 1} {self.type_name} "
                f"@ {self.start_lba} ({human_size(self.size_bytes)})")


def _chs(lba: int) -> bytes:
    """Encode a CHS address, saturating at the classic 1023/254/63 maximum."""
    heads, sectors = 255, 63
    cylinder, rest = divmod(lba, heads * sectors)
    head, sector = divmod(rest, sectors)
    if cylinder > 1023:
        cylinder, head, sector = 1023, 254, 62
    return bytes([head, ((cylinder >> 2) & 0xC0) | (sector + 1), cylinder & 0xFF])


def read_table(handle: BinaryIO) -> list[MbrPartition]:
    handle.seek(0)
    sector = handle.read(SECTOR)
    if len(sector) < SECTOR or sector[SIGNATURE_OFFSET:SIGNATURE_OFFSET + 2] != b"\x55\xaa":
        raise ValueError("no MBR signature found")
    parts = []
    for index in range(4):
        raw = sector[TABLE_OFFSET + index * 16: TABLE_OFFSET + (index + 1) * 16]
        boot, type_id = raw[0], raw[4]
        start, count = struct.unpack_from("<II", raw, 8)
        parts.append(MbrPartition(index, boot, type_id, start, count))
    return parts


def write_table(handle: BinaryIO, partitions: list[MbrPartition],
                disk_id: int | None = None) -> None:
    """Write the four table slots, preserving any existing boot code."""
    handle.seek(0)
    sector = bytearray(handle.read(SECTOR))
    if len(sector) < SECTOR:
        sector = bytearray(SECTOR)
    if disk_id is not None:
        struct.pack_into("<I", sector, 440, disk_id & 0xFFFFFFFF)
    for index in range(4):
        entry = bytearray(16)
        match = next((p for p in partitions if p.index == index and not p.empty), None)
        if match is not None:
            entry[0] = match.boot_flag
            entry[1:4] = _chs(match.start_lba)
            entry[4] = match.type_id
            entry[5:8] = _chs(match.start_lba + match.sector_count - 1)
            struct.pack_into("<II", entry, 8, match.start_lba, match.sector_count)
        sector[TABLE_OFFSET + index * 16: TABLE_OFFSET + (index + 1) * 16] = entry
    sector[SIGNATURE_OFFSET:SIGNATURE_OFFSET + 2] = b"\x55\xaa"
    handle.seek(0)
    handle.write(bytes(sector))


def describe(partitions: list[MbrPartition]) -> str:
    lines = [str(p) for p in partitions if not p.empty]
    return "\n".join(lines) if lines else "(no partitions)"
