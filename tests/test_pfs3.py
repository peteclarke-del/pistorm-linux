"""Tests for PFS3 reading and volume creation.

The reader is exercised against real PFS3 volumes when an image is available;
those tests skip otherwise.
"""
import os
import shutil
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

    def test_anode_arithmetic_matches_the_reference(self):
        volume = self.volume(0)
        self.assertEqual(volume.anodes_per_block, 84)     # (1024-16)/12
        self.assertEqual(volume.index_per_block, 253)     # (1024-12)/4
        self.assertTrue(volume.split_anodes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
