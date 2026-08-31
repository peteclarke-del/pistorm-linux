"""Recognising a prepared system from the image a user points at."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import amigaos, amigafs, builder, distributions, rdb  # noqa: E402
from pistorm_imager.core.util import GIB, MIB  # noqa: E402


class TestIdentify(unittest.TestCase):
    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-dist-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder

    def image_with_volume(self, label: str) -> Path:
        """A minimal Amiga drive whose boot volume carries ``label``."""
        path = self.scratch() / "system.hdf"
        geometry = rdb.Geometry()
        size = 40 * MIB
        table = rdb.Rdb(
            geometry=geometry,
            partitions=[rdb.Partition("SDH0", 1, 30, rdb.DOSTYPE_FFS_INTL,
                                      bootable=True)],
            filesystems=[],
            cylinders=(size // 512) // geometry.cyl_blocks)
        with open(path, "wb") as handle:
            handle.truncate(size)
            table.write(handle, 0)
        with open(path, "r+b") as handle:
            part = table.partitions[0]
            volume = amigaos.make_volume(handle, part.byte_offset(geometry),
                                         part.blocks(geometry), label,
                                         amigafs.DOSTYPE_FFS_INTL)
            volume.close()
        return path

    def test_a_caffeineos_image_is_recognised_by_its_volume(self):
        found = distributions.identify(self.image_with_volume("CaffeineOS"))
        self.assertIsNotNone(found)
        self.assertEqual(found.key, "caffeineos")
        self.assertTrue(found.emu68_native)
        self.assertTrue(found.rtg_only)

    def test_the_match_ignores_case(self):
        found = distributions.identify(self.image_with_volume("caffeineos"))
        self.assertIsNotNone(found)

    def test_an_unknown_system_is_not_guessed_at(self):
        self.assertIsNone(distributions.identify(self.image_with_volume("Workbench")))

    def test_a_file_with_no_amiga_drive_identifies_as_nothing(self):
        path = self.scratch() / "empty.img"
        path.write_bytes(b"\0" * 8192)
        self.assertIsNone(distributions.identify(path))

    def test_the_description_warns_about_a_card_that_is_too_small(self):
        found = distributions.CATALOGUE_BY_KEY["caffeineos"]
        big = distributions.describe(found, 128 * GIB)
        small = distributions.describe(found, 16 * GIB)
        self.assertFalse(any("smaller than" in line for line in big))
        self.assertTrue(any("smaller than" in line for line in small))

    def test_every_entry_says_where_to_get_it(self):
        for distribution in distributions.CATALOGUE:
            self.assertTrue(distribution.home.startswith("http"),
                            distribution.key)
            self.assertTrue(distribution.summary, distribution.key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
