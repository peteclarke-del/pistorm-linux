"""A minimal FAT32 reader/writer that works directly on an image file.

The Emu68 boot partition is plain FAT32, and every file we need to put on it
(the Emu68 kernel, Raspberry Pi firmware, config.txt, a Kickstart ROM) is
written once and never modified in place.  That narrow requirement means we can
avoid depending on ``mtools`` and, more importantly, avoid needing root to loop
mount anything: we format with ``mkfs.vfat`` (which is happy to operate on a
regular file) and then inject files ourselves.

Long file names are supported because they are not optional here - the Emu68
kernel is called e.g. ``Emu68-pistorm32lite`` and the device tree blobs have
names like ``bcm2711-rpi-4-b.dtb``.
"""
from __future__ import annotations

import dataclasses
import os
import struct
import time
from typing import BinaryIO, Iterator

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LONG_NAME = 0x0F

FREE_CLUSTER = 0x00000000
BAD_CLUSTER = 0x0FFFFFF7
END_OF_CHAIN = 0x0FFFFFFF
CLUSTER_MASK = 0x0FFFFFFF

_INVALID_SHORT = set('"*+,/:;<=>?[\\]|')
# Bits in the reserved byte of a short entry that tell the OS to display the
# base name and/or extension in lower case.  Using them avoids emitting a long
# name entry for something like "config.txt", which is already a valid 8.3 name.
NT_LOWER_BASE = 0x08
NT_LOWER_EXT = 0x10


class Fat32Error(RuntimeError):
    pass


@dataclasses.dataclass
class DirEntry:
    name: str
    attr: int
    cluster: int
    size: int
    #  Where the 32-byte short entry lives, so we can rewrite it after a write.
    entry_offset: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attr & ATTR_DIRECTORY)


def _fat_time(ts: float | None = None) -> tuple[int, int]:
    """Return (time, date) words in FAT format."""
    lt = time.localtime(ts if ts is not None else time.time())
    year = max(1980, lt.tm_year)
    fdate = ((year - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday
    ftime = (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)
    return ftime, fdate


def _lfn_checksum(short_name: bytes) -> int:
    total = 0
    for byte in short_name:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


class Fat32:
    """Read/write access to a FAT32 filesystem inside ``image`` at ``offset``."""

    def __init__(self, image: BinaryIO, offset: int = 0):
        self.f = image
        self.base = offset
        self._read_bpb()
        self._next_free_hint = 2

    # ---------------------------------------------------------------- layout

    def _read_bpb(self) -> None:
        self.f.seek(self.base)
        bpb = self.f.read(512)
        if len(bpb) < 512 or bpb[510:512] != b"\x55\xaa":
            raise Fat32Error("no boot sector signature - not a FAT filesystem")
        self.bytes_per_sector = struct.unpack_from("<H", bpb, 0x0B)[0]
        self.sectors_per_cluster = bpb[0x0D]
        self.reserved_sectors = struct.unpack_from("<H", bpb, 0x0E)[0]
        self.num_fats = bpb[0x10]
        root_entries = struct.unpack_from("<H", bpb, 0x11)[0]
        fat_size16 = struct.unpack_from("<H", bpb, 0x16)[0]
        total16 = struct.unpack_from("<H", bpb, 0x13)[0]
        total32 = struct.unpack_from("<I", bpb, 0x20)[0]
        self.fat_size = struct.unpack_from("<I", bpb, 0x24)[0] or fat_size16
        self.root_cluster = struct.unpack_from("<I", bpb, 0x2C)[0]
        self.fsinfo_sector = struct.unpack_from("<H", bpb, 0x30)[0]
        self.total_sectors = total32 or total16
        if not self.bytes_per_sector or not self.sectors_per_cluster:
            raise Fat32Error("invalid BPB (zero sector or cluster size)")
        if root_entries != 0 or self.fat_size == 0 or self.root_cluster < 2:
            raise Fat32Error("filesystem is not FAT32")
        self.cluster_bytes = self.bytes_per_sector * self.sectors_per_cluster
        self.fat_offset = self.base + self.reserved_sectors * self.bytes_per_sector
        data_sector = self.reserved_sectors + self.num_fats * self.fat_size
        self.data_offset = self.base + data_sector * self.bytes_per_sector
        self.cluster_count = (self.total_sectors - data_sector) // self.sectors_per_cluster
        self.max_cluster = self.cluster_count + 1  # clusters are numbered from 2
        # Keep the whole FAT in memory.  Even for a large boot partition it is
        # only a few hundred KiB, and seeking per entry made writes glacial.
        self.f.seek(self.fat_offset)
        self._fat = bytearray(self.f.read(self.fat_size * self.bytes_per_sector))
        self._fat_dirty = False

    def cluster_offset(self, cluster: int) -> int:
        return self.data_offset + (cluster - 2) * self.cluster_bytes

    @property
    def free_bytes(self) -> int:
        free = sum(1 for c in range(2, self.max_cluster + 1)
                   if self._get_fat(c) == FREE_CLUSTER)
        return free * self.cluster_bytes

    # ------------------------------------------------------------------- FAT

    def _get_fat(self, cluster: int) -> int:
        return struct.unpack_from("<I", self._fat, cluster * 4)[0] & CLUSTER_MASK

    def _set_fat(self, cluster: int, value: int) -> None:
        old = struct.unpack_from("<I", self._fat, cluster * 4)[0]
        # The top 4 bits of a FAT32 entry are reserved and must be preserved.
        struct.pack_into("<I", self._fat, cluster * 4,
                         (old & 0xF0000000) | (value & CLUSTER_MASK))
        self._fat_dirty = True

    def _write_fat(self) -> None:
        if not self._fat_dirty:
            return
        for fat in range(self.num_fats):
            self.f.seek(self.fat_offset + fat * self.fat_size * self.bytes_per_sector)
            self.f.write(self._fat)
        self._fat_dirty = False

    def _chain(self, cluster: int) -> Iterator[int]:
        seen = set()
        while 2 <= cluster < BAD_CLUSTER:
            if cluster in seen:
                raise Fat32Error("cyclic cluster chain")
            seen.add(cluster)
            yield cluster
            cluster = self._get_fat(cluster)

    def _find_free(self, start: int) -> int | None:
        for cluster in range(max(2, start), self.max_cluster + 1):
            if self._get_fat(cluster) == FREE_CLUSTER:
                return cluster
        for cluster in range(2, min(start, self.max_cluster + 1)):
            if self._get_fat(cluster) == FREE_CLUSTER:
                return cluster
        return None

    def _alloc_cluster(self, previous: int | None = None, *, zero: bool = True) -> int:
        cluster = self._find_free(self._next_free_hint)
        if cluster is None:
            raise Fat32Error("FAT32 partition is full")
        self._set_fat(cluster, END_OF_CHAIN)
        if previous is not None:
            self._set_fat(previous, cluster)
        self._next_free_hint = cluster + 1
        if zero:
            self._zero_cluster(cluster)
        return cluster

    def _alloc_run(self, count: int, previous: int | None = None) -> tuple[int, int]:
        """Allocate up to ``count`` contiguous clusters; returns (first, length).

        Writing a run in one ``write()`` instead of one call per cluster is the
        difference between a couple of seconds and several minutes when copying
        a 3 MB Emu68 kernel onto the boot partition.
        """
        first = self._find_free(self._next_free_hint)
        if first is None:
            raise Fat32Error("FAT32 partition is full")
        length = 1
        while length < count and first + length <= self.max_cluster \
                and self._get_fat(first + length) == FREE_CLUSTER:
            length += 1
        for index in range(length):
            cluster = first + index
            self._set_fat(cluster, END_OF_CHAIN if index == length - 1 else cluster + 1)
        if previous is not None:
            self._set_fat(previous, first)
        self._next_free_hint = first + length
        return first, length

    def _zero_cluster(self, cluster: int) -> None:
        self.f.seek(self.cluster_offset(cluster))
        self.f.write(b"\0" * self.cluster_bytes)

    # ----------------------------------------------------------- directories

    def _read_dir_raw(self, cluster: int) -> list[tuple[int, bytes]]:
        """Return [(absolute offset, 32-byte entry)] for a directory chain."""
        out: list[tuple[int, bytes]] = []
        for clu in self._chain(cluster):
            base = self.cluster_offset(clu)
            self.f.seek(base)
            data = self.f.read(self.cluster_bytes)
            for i in range(0, len(data), 32):
                out.append((base + i, data[i:i + 32]))
        return out

    def _iter_entries(self, cluster: int) -> Iterator[DirEntry]:
        long_parts: dict[int, str] = {}
        for offset, raw in self._read_dir_raw(cluster):
            if raw[0] == 0x00:
                return
            if raw[0] == 0xE5:
                long_parts.clear()
                continue
            attr = raw[11]
            if attr & ATTR_LONG_NAME == ATTR_LONG_NAME:
                seq = raw[0] & 0x3F
                chars = raw[1:11] + raw[14:26] + raw[28:32]
                text = chars.decode("utf-16-le", errors="ignore")
                text = text.split("￿")[0].split("\x00")[0]
                long_parts[seq] = text
                continue
            if attr & ATTR_VOLUME_ID:
                long_parts.clear()
                continue
            if long_parts:
                name = "".join(long_parts[k] for k in sorted(long_parts))
            else:
                stem = raw[0:8].decode("latin-1").rstrip()
                ext = raw[8:11].decode("latin-1").rstrip()
                if raw[12] & NT_LOWER_BASE:
                    stem = stem.lower()
                if raw[12] & NT_LOWER_EXT:
                    ext = ext.lower()
                name = f"{stem}.{ext}" if ext else stem
            long_parts.clear()
            cluster_no = (struct.unpack_from("<H", raw, 20)[0] << 16) | struct.unpack_from("<H", raw, 26)[0]
            size = struct.unpack_from("<I", raw, 28)[0]
            yield DirEntry(name, attr, cluster_no, size, offset)

    def listdir(self, path: str = "/") -> list[DirEntry]:
        cluster = self._resolve_dir(path)
        return [e for e in self._iter_entries(cluster) if e.name not in (".", "..")]

    def _resolve_dir(self, path: str) -> int:
        cluster = self.root_cluster
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            entry = self._find(cluster, part)
            if entry is None or not entry.is_dir:
                raise Fat32Error(f"directory not found: {path}")
            cluster = entry.cluster or self.root_cluster
        return cluster

    def _find(self, dir_cluster: int, name: str) -> DirEntry | None:
        lowered = name.lower()
        for entry in self._iter_entries(dir_cluster):
            if entry.name.lower() == lowered:
                return entry
        return None

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except Fat32Error:
            return False

    def stat(self, path: str) -> DirEntry:
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            raise Fat32Error("cannot stat the root directory")
        parent = self._resolve_dir("/".join(parts[:-1]))
        entry = self._find(parent, parts[-1])
        if entry is None:
            raise Fat32Error(f"not found: {path}")
        return entry

    # ------------------------------------------------------- name generation

    @staticmethod
    def _clean(part: str) -> str:
        return "".join("_" if c in _INVALID_SHORT or ord(c) < 0x20 or ord(c) > 0x7E
                       else c for c in part)

    def _short_name(self, dir_cluster: int, name: str) -> tuple[bytes, bool, int]:
        """Build an 8.3 name.

        Returns ``(11 raw bytes, needs_long_name, nt_case_flags)``.  A name that
        already fits 8.3 apart from its case needs no long name entry - the two
        case bits in the reserved byte cover that, which is how Windows itself
        stores ``config.txt``.
        """
        base, dot, ext = name.rpartition(".")
        if not dot:
            base, ext = name, ""
        c_base, c_ext = self._clean(base), self._clean(ext)
        fits = (0 < len(c_base) <= 8 and len(c_ext) <= 3 and c_base == base
                and c_ext == ext and " " not in name and "." not in base)

        nt_flags = 0
        if fits:
            # Only pure lower or pure upper case can be represented by the flags.
            if c_base.islower():
                nt_flags |= NT_LOWER_BASE
            elif not c_base.isupper() and c_base.upper() != c_base:
                fits = False
            if c_ext.islower():
                nt_flags |= NT_LOWER_EXT
            elif c_ext and not c_ext.isupper() and c_ext.upper() != c_ext:
                fits = False

        stem = (c_base.replace(".", "").replace(" ", "") or "_").upper()
        ext_up = c_ext.replace(" ", "")[:3].upper()
        existing = {bytes(raw[0:11]) for _, raw in self._read_dir_raw(dir_cluster)
                    if raw[0] not in (0x00, 0xE5)
                    and raw[11] & ATTR_LONG_NAME != ATTR_LONG_NAME}
        if fits:
            candidate = f"{stem:<8}{ext_up:<3}".encode("latin-1")
            if candidate not in existing:
                return candidate, False, nt_flags
        for index in range(1, 1000):
            suffix = f"~{index}"
            candidate = f"{stem[: 8 - len(suffix)] + suffix:<8}{ext_up:<3}".encode("latin-1")
            if candidate not in existing:
                return candidate, True, 0
        raise Fat32Error(f"cannot generate a unique short name for {name!r}")

    # --------------------------------------------------------- entry writing

    def _dir_clusters(self, dir_cluster: int) -> list[int]:
        return list(self._chain(dir_cluster))

    def _alloc_dir_slots(self, dir_cluster: int, count: int) -> list[int]:
        """Find ``count`` consecutive free 32-byte slots, growing the dir if needed."""
        while True:
            raw = self._read_dir_raw(dir_cluster)
            run: list[int] = []
            for offset, entry in raw:
                if entry[0] in (0x00, 0xE5):
                    run.append(offset)
                    if len(run) == count:
                        return run
                else:
                    run = []
            last = self._dir_clusters(dir_cluster)[-1]
            self._alloc_cluster(previous=last)

    def _write_entry(self, dir_cluster: int, name: str, attr: int,
                     first_cluster: int, size: int) -> int:
        short, needs_long, nt_flags = self._short_name(dir_cluster, name)
        long_entries: list[bytes] = []
        if needs_long:
            checksum = _lfn_checksum(short)
            padded = name + "\0"
            chunks = [padded[i:i + 13] for i in range(0, len(padded), 13)]
            if len(chunks[-1]) < 13:
                chunks[-1] = chunks[-1] + "￿" * (13 - len(chunks[-1]))
            for index, chunk in enumerate(chunks, start=1):
                encoded = chunk.encode("utf-16-le")
                order = index | (0x40 if index == len(chunks) else 0)
                entry = bytearray(32)
                entry[0] = order
                entry[1:11] = encoded[0:10]
                entry[11] = ATTR_LONG_NAME
                entry[12] = 0
                entry[13] = checksum
                entry[14:26] = encoded[10:22]
                entry[26:28] = b"\0\0"
                entry[28:32] = encoded[22:26]
                long_entries.append(bytes(entry))
            long_entries.reverse()

        ftime, fdate = _fat_time()
        short_entry = bytearray(32)
        short_entry[0:11] = short
        short_entry[11] = attr
        short_entry[12] = nt_flags
        struct.pack_into("<H", short_entry, 14, ftime)
        struct.pack_into("<H", short_entry, 16, fdate)
        struct.pack_into("<H", short_entry, 18, fdate)
        struct.pack_into("<H", short_entry, 20, (first_cluster >> 16) & 0xFFFF)
        struct.pack_into("<H", short_entry, 22, ftime)
        struct.pack_into("<H", short_entry, 24, fdate)
        struct.pack_into("<H", short_entry, 26, first_cluster & 0xFFFF)
        struct.pack_into("<I", short_entry, 28, size)

        slots = self._alloc_dir_slots(dir_cluster, len(long_entries) + 1)
        for slot, payload in zip(slots, long_entries + [bytes(short_entry)]):
            self.f.seek(slot)
            self.f.write(payload)
        return slots[-1]

    # ------------------------------------------------------------- public IO

    def makedirs(self, path: str) -> int:
        """Create ``path`` and any missing parents; returns its first cluster."""
        cluster = self.root_cluster
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            found = self._find(cluster, part)
            if found is not None:
                if not found.is_dir:
                    raise Fat32Error(f"{part} exists and is not a directory")
                cluster = found.cluster
                continue
            new_cluster = self._alloc_cluster()
            self._write_entry(cluster, part, ATTR_DIRECTORY, new_cluster, 0)
            ftime, fdate = _fat_time()
            dot = bytearray(32)
            dot[0:11] = b".          "
            dot[11] = ATTR_DIRECTORY
            struct.pack_into("<H", dot, 20, (new_cluster >> 16) & 0xFFFF)
            struct.pack_into("<H", dot, 26, new_cluster & 0xFFFF)
            struct.pack_into("<H", dot, 24, fdate)
            dotdot = bytearray(dot)
            dotdot[0:11] = b"..         "
            parent = 0 if cluster == self.root_cluster else cluster
            struct.pack_into("<H", dotdot, 20, (parent >> 16) & 0xFFFF)
            struct.pack_into("<H", dotdot, 26, parent & 0xFFFF)
            self.f.seek(self.cluster_offset(new_cluster))
            self.f.write(bytes(dot) + bytes(dotdot))
            cluster = new_cluster
        return cluster

    def remove(self, path: str) -> None:
        """Free a file's clusters and mark its directory entries deleted."""
        entry = self.stat(path)
        if entry.is_dir:
            raise Fat32Error("remove() does not delete directories")
        if entry.cluster >= 2:
            for cluster in list(self._chain(entry.cluster)):
                self._set_fat(cluster, FREE_CLUSTER)
            self._next_free_hint = 2
        # Walk backwards over the preceding long-name entries and blank them.
        offset = entry.entry_offset
        self.f.seek(offset)
        self.f.write(b"\xe5")
        probe = offset - 32
        while probe >= self.data_offset:
            self.f.seek(probe)
            raw = self.f.read(32)
            if raw[11] != ATTR_LONG_NAME or raw[0] in (0x00, 0xE5):
                break
            self.f.seek(probe)
            self.f.write(b"\xe5")
            if raw[0] & 0x40:
                break
            probe -= 32

    def write_bytes(self, path: str, data: bytes, *, overwrite: bool = True) -> None:
        self.write_stream(path, len(data), iter([data]), overwrite=overwrite)

    def write_file(self, path: str, source: str | os.PathLike, *,
                   overwrite: bool = True, chunk: int = 1 << 20) -> None:
        size = os.path.getsize(source)

        def chunks():
            with open(source, "rb") as handle:
                while True:
                    buf = handle.read(chunk)
                    if not buf:
                        return
                    yield buf

        self.write_stream(path, size, chunks(), overwrite=overwrite)

    def write_stream(self, path: str, size: int, chunks, *, overwrite: bool = True) -> None:
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            raise Fat32Error("no file name given")
        parent = self.makedirs("/".join(parts[:-1])) if len(parts) > 1 else self.root_cluster
        name = parts[-1]
        existing = self._find(parent, name)
        if existing is not None:
            if not overwrite:
                raise Fat32Error(f"{path} already exists")
            self.remove(path)

        first = 0
        current = 0
        buffer = bytearray()
        written = 0

        def flush(force: bool) -> None:
            nonlocal first, current, buffer, written
            while len(buffer) >= self.cluster_bytes or (force and buffer):
                want = max(1, len(buffer) // self.cluster_bytes)
                start, length = self._alloc_run(want, previous=current or None)
                take = min(len(buffer), length * self.cluster_bytes)
                block = bytes(buffer[:take])
                del buffer[:take]
                if len(block) % self.cluster_bytes:
                    pad = self.cluster_bytes - (len(block) % self.cluster_bytes)
                    block += b"\0" * pad
                self.f.seek(self.cluster_offset(start))
                self.f.write(block)
                if not first:
                    first = start
                current = start + length - 1
                written += take

        for piece in chunks:
            buffer += piece
            flush(False)
        flush(True)
        if written != size:
            raise Fat32Error(f"size mismatch writing {path}: {written} != {size}")
        self._write_entry(parent, name, ATTR_ARCHIVE, first, size)
        self._invalidate_fsinfo()
        self._write_fat()

    def read_bytes(self, path: str) -> bytes:
        entry = self.stat(path)
        if entry.is_dir:
            raise Fat32Error(f"{path} is a directory")
        out = bytearray()
        if entry.cluster >= 2:
            for cluster in self._chain(entry.cluster):
                self.f.seek(self.cluster_offset(cluster))
                out += self.f.read(self.cluster_bytes)
                if len(out) >= entry.size:
                    break
        return bytes(out[: entry.size])

    def _invalidate_fsinfo(self) -> None:
        """Mark the cached free-cluster count unknown so the OS recounts it."""
        if not self.fsinfo_sector:
            return
        pos = self.base + self.fsinfo_sector * self.bytes_per_sector
        self.f.seek(pos)
        if self.f.read(4) != b"RRaA":
            return
        self.f.seek(pos + 488)
        self.f.write(struct.pack("<II", 0xFFFFFFFF, 0xFFFFFFFF))

    def flush(self) -> None:
        self._write_fat()
        self.f.flush()
