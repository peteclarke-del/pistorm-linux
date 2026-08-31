"""Tests for PFS3 reading and volume creation.

The reader is exercised against real PFS3 volumes when an image is available;
those tests skip otherwise.
"""
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import pfs3, rdb  # noqa: E402
from pistorm_imager.core.util import MIB, Progress  # noqa: E402

QUIET = Progress()

#  A real multi-partition PFS3 image, if the NAS happens to be mounted.
REAL_IMAGE = Path(
    "/run/user/1000/gvfs/smb-share:server=synologynas,share=emulation/Computer/"
    "Commodore/Commodore Amiga/Emulation Installations/HstWB/120gb.hdf")
HAVE_REAL = REAL_IMAGE.is_file()


class _Scratch(unittest.TestCase):
    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-pfs3-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder

    def new_volume(self, size: int = 256 * MIB, name: str = "Test"):
        path = self.scratch() / "vol.hdf"
        handle = open(path, "w+b")
        self.addCleanup(handle.close)
        handle.truncate(size)
        writer = pfs3.Pfs3Writer(handle, 0, size // pfs3.SECTOR, name)
        writer.format()
        return writer, handle, path


class TestFormatArithmetic(unittest.TestCase):
    def test_mode_flags_match_a_real_volume(self):
        """format.c's option set is exactly what real volumes carry."""
        self.assertEqual(pfs3.FORMAT_OPTIONS, 0x77F)
        self.assertEqual(pfs3.FORMAT_OPTIONS | pfs3.MODE_SUPERINDEX, 0x7FF)

    def test_superindex_only_above_the_small_disk_limit(self):
        limit = pfs3.max_small_disk(1024)
        self.assertFalse(pfs3.max_small_disk(1024) < 4095504)   # a 2 GiB volume
        self.assertGreater(109276272, limit)                    # a 52 GiB volume

    def test_reserved_count_is_a_multiple_of_32(self):
        for sectors in (2 * 1024 * 1024, 4095504, 109276272):
            self.assertEqual(pfs3.calc_num_reserved(sectors, 1024) % 32, 0)

    def test_bitmap_payload(self):
        self.assertEqual(pfs3.bitmap_payload(1024), 253)
        self.assertEqual(pfs3.bitmap_payload(2048), 509)


class TestVolumeCreation(_Scratch):
    def test_empty_volume_reads_back(self):
        writer, handle, path = self.new_volume(name="Empty")
        writer.close()
        handle.flush()
        volume = pfs3.Pfs3Volume(open(path, "rb"))
        self.addCleanup(volume.f.close)
        self.assertEqual(volume.name, "Empty")
        self.assertEqual(volume.options, pfs3.FORMAT_OPTIONS)
        self.assertEqual(volume.reserved_blksize, 1024)
        self.assertEqual(volume.listdir(), [])

    def test_files_and_directories_round_trip(self):
        writer, handle, path = self.new_volume()
        libs = writer.makedirs("Libs")
        payload = os.urandom(300_000)
        writer.write_file(libs, "big.library", payload)
        writer.write_file(writer.root_anode, "Startup-Sequence", b"echo hi\n")
        writer.makedirs("S/Deep/Nested")
        writer.close()
        handle.flush()

        volume = pfs3.Pfs3Volume(open(path, "rb"))
        self.addCleanup(volume.f.close)
        self.assertEqual(volume.read_file(volume.find("Libs/big.library")), payload)
        self.assertEqual(volume.read_file(volume.find("Startup-Sequence")),
                         b"echo hi\n")
        self.assertIsNotNone(volume.find("S/Deep/Nested"))

    def test_directory_larger_than_one_block(self):
        """More entries than a dirblock holds must chain onto another."""
        writer, handle, path = self.new_volume()
        folder = writer.makedirs("C")
        names = [f"Command{i:03}" for i in range(120)]
        for name in names:
            writer.write_file(folder, name, name.encode())
        writer.close()
        handle.flush()

        volume = pfs3.Pfs3Volume(open(path, "rb"))
        self.addCleanup(volume.f.close)
        entry = volume.find("C")
        found = {e.name for e in volume.listdir(entry.anode)}
        self.assertEqual(found, set(names))
        self.assertEqual(volume.read_file(volume.find("C/Command042")),
                         b"Command042")

    def test_empty_file(self):
        writer, handle, path = self.new_volume()
        writer.write_file(writer.root_anode, "empty", b"")
        writer.close()
        handle.flush()
        volume = pfs3.Pfs3Volume(open(path, "rb"))
        self.addCleanup(volume.f.close)
        self.assertEqual(volume.read_file(volume.find("empty")), b"")

    def test_reserved_anodes_are_marked_taken(self):
        """Anodes 0..4 carry blocknr 0xFFFFFFFF on a real volume."""
        writer, handle, path = self.new_volume()
        writer.close()
        handle.flush()
        volume = pfs3.Pfs3Volume(open(path, "rb"))
        self.addCleanup(volume.f.close)
        for number in range(pfs3.ANODE_ROOTDIR):
            self.assertEqual(volume.anode(number).blocknr,
                             pfs3.ANODE_RESERVED_BLOCKNR)

    def test_a_volume_past_the_small_disk_limit_round_trips(self):
        """Above max_small_disk the anode index moves behind super blocks.

        5 GiB is the smallest size that turns SUPERINDEX on.  The file is
        sparse, so only the metadata actually written costs anything - and
        without this the whole large-volume path went untested, which is how
        every partition of an SD-card build came out unmountable.
        """
        size = 5 * 1024 * MIB
        self.assertGreater(size // pfs3.SECTOR, pfs3.max_small_disk(1024))
        writer, handle, path = self.new_volume(size, name="Big")
        payload = os.urandom(200_000)
        writer.write_file(writer.makedirs("Libs"), "big.library", payload)
        writer.write_file(writer.root_anode, "Startup-Sequence", b"echo hi\n")
        writer.close()
        handle.flush()

        volume = pfs3.Pfs3Volume(open(path, "rb"))
        self.addCleanup(volume.f.close)
        self.assertTrue(volume.superindex)
        self.assertEqual(volume.options, 0x7FF)
        self.assertTrue(any(volume.super_index))
        self.assertEqual(volume.read_file(volume.find("Libs/big.library")),
                         payload)
        self.assertEqual(volume.read_file(volume.find("Startup-Sequence")),
                         b"echo hi\n")

    def test_entries_carry_the_extra_fields_word(self):
        """MODE_DIR_EXTENSION puts a bitmask in the last two bytes of an entry.

        Odd and even name lengths are both checked: only an odd one puts a
        name character where the handler looks for that bitmask.
        """
        writer, handle, path = self.new_volume()
        for name in ("C", "Libs", "workbench.library", "icon.library"):
            writer.write_file(writer.root_anode, name, b"x")
        writer.close()
        handle.flush()

        volume = pfs3.Pfs3Volume(open(path, "rb"))
        self.addCleanup(volume.f.close)
        block = volume._reserved(volume.anode(pfs3.ANODE_ROOTDIR).blocknr)
        offset = pfs3.SIZEOF_DIRBLOCK_HEADER
        seen = 0
        while offset < volume.reserved_blksize and block[offset]:
            size = block[offset]
            nlength = block[offset + 17]
            expected = 18 + nlength + 1        # header, name, comment length
            expected += expected % 2           # word aligned
            expected += 2                      # the extra fields bitmask
            self.assertEqual(size, expected)
            self.assertEqual(size % 2, 0)
            self.assertEqual(block[offset + size - 2:offset + size], b"\0\0")
            offset += size
            seen += 1
        self.assertEqual(seen, 4)

    def test_the_bitmap_covers_the_whole_volume(self):
        """Bit n is block n of the partition, reserved area included.

        The handler works the block count out from ``disksize``, so a bitmap
        sized from the data area alone leaves it following a null pointer for
        the last stretch of a large disk.
        """
        writer, handle, path = self.new_volume(512 * MIB)
        writer.write_file(writer.root_anode, "a.bin", os.urandom(100_000))
        writer.close()
        handle.flush()

        volume = pfs3.Pfs3Volume(open(path, "rb"))
        self.addCleanup(volume.f.close)
        per_block = 32 * pfs3.bitmap_payload(volume.reserved_blksize)
        expected = (volume.disksize + per_block - 1) // per_block
        blocks = []
        for index_sector in [b for b in volume.bitmap_index if b]:
            index = volume._reserved(index_sector)
            self.assertEqual(index[0:2], pfs3.ID_BITMAPINDEX)
            blocks += [p for p in struct.unpack_from(
                f">{volume.index_per_block}I", index,
                pfs3.SIZEOF_INDEXBLOCK_HEADER) if p]
        self.assertEqual(len(blocks), expected)

        first = volume._reserved(blocks[0])
        self.assertEqual(first[0:2], pfs3.ID_BITMAPBLOCK)
        #  The boot block and the reserved area are at the bottom of the disk
        #  and must read as taken.
        self.assertEqual(
            struct.unpack_from(">I", first, pfs3.SIZEOF_INDEXBLOCK_HEADER)[0], 0)

        def free(block_number: int) -> bool:
            seq, rest = divmod(block_number, per_block)
            long_index, bit = divmod(rest, 32)
            data = volume._reserved(blocks[seq])
            value = struct.unpack_from(
                ">I", data,
                pfs3.SIZEOF_INDEXBLOCK_HEADER + long_index * 4)[0]
            return bool(value & (0x80000000 >> bit))

        self.assertFalse(free(0), "the boot block is not free")
        self.assertFalse(free(volume.lastreserved), "reserved blocks are not free")
        self.assertTrue(free(volume.disksize - 1), "the last block is free")

    def test_free_space_shrinks_as_files_are_written(self):
        writer, handle, _path = self.new_volume()
        before = writer.free_bytes
        writer.write_file(writer.root_anode, "a.bin", os.urandom(1_000_000))
        self.assertLess(writer.free_bytes, before)
        writer.close()


@unittest.skipUnless(HAVE_REAL, "no real PFS3 image available")
class TestRealVolumes(unittest.TestCase):
    """The reader against volumes produced by the actual Amiga file system."""

    @classmethod
    def setUpClass(cls):
        cls.handle = open(REAL_IMAGE, "rb")
        cls.table = rdb.Rdb.read(cls.handle, 0)

    @classmethod
    def tearDownClass(cls):
        cls.handle.close()

    def volume(self, index: int) -> pfs3.Pfs3Volume:
        part = self.table.partitions[index]
        offset = part.start_block(self.table.geometry) * pfs3.SECTOR
        return pfs3.Pfs3Volume(self.handle, offset)

    def test_reads_a_small_index_volume(self):
        volume = self.volume(0)
        self.assertEqual(volume.name, "Workbench")
        self.assertFalse(volume.superindex)
        self.assertEqual(volume.options, 0x77F)
        self.assertEqual(volume.reserved_blksize, 1024)
        self.assertTrue(volume.listdir())

    def test_reads_a_superindex_volume(self):
        volume = self.volume(1)
        self.assertTrue(volume.superindex)
        self.assertEqual(volume.options, 0x7FF)
        self.assertTrue(volume.listdir())

    def test_every_file_reads_at_its_stated_length(self):
        volume = self.volume(0)
        files = [(p, e) for p, e in volume.walk() if e.is_file]
        self.assertGreater(len(files), 20)
        for path, entry in files:
            self.assertEqual(len(volume.read_file(entry)), entry.size, path)

    def test_real_entries_end_with_the_extra_fields_word(self):
        """The layout the writer follows is the one real volumes use.

        Nothing in the header says an entry ends with a two-byte bitmask; it
        only shows up when a real entry is measured against its parts.
        """
        volume = self.volume(0)
        self.assertTrue(volume.dir_extension)
        block = volume._reserved(volume.anode(pfs3.ANODE_ROOTDIR).blocknr)
        offset = pfs3.SIZEOF_DIRBLOCK_HEADER
        seen = 0
        while offset < volume.reserved_blksize and block[offset]:
            size = block[offset]
            nlength = block[offset + 17]
            comment = block[offset + 18 + nlength]
            expected = 18 + nlength + 1 + comment
            expected += expected % 2
            expected += 2
            self.assertEqual(size, expected)
            offset += size
            seen += 1
        self.assertGreaterEqual(seen, 5)

    def test_a_real_bitmap_is_indexed_from_block_zero(self):
        """The convention the writer follows, read off a real volume."""
        volume = self.volume(1)                  # the 52 GiB superindex one
        index = volume._reserved([b for b in volume.bitmap_index if b][0])
        first = struct.unpack_from(">I", index,
                                   pfs3.SIZEOF_INDEXBLOCK_HEADER)[0]
        block = volume._reserved(first)
        self.assertEqual(block[0:2], pfs3.ID_BITMAPBLOCK)
        self.assertEqual(
            struct.unpack_from(">I", block, pfs3.SIZEOF_INDEXBLOCK_HEADER)[0], 0,
            "block 0 is the boot block, so the first long cannot be all free")

    def test_anode_arithmetic_matches_the_reference(self):
        volume = self.volume(0)
        self.assertEqual(volume.anodes_per_block, 84)     # (1024-16)/12
        self.assertEqual(volume.index_per_block, 253)     # (1024-12)/4
        self.assertTrue(volume.split_anodes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
