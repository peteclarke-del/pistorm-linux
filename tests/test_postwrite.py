"""Adapting a prepared system on the card once it has been written."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import pfs3, postwrite, rdb  # noqa: E402
from pistorm_imager.core.util import MIB, Progress  # noqa: E402

QUIET = Progress()
#  A real ScreenMode preference: an IFF FORM, which is what makes the blanked
#  version recognisably not one.
PREFS = b"FORM\x00\x00\x006PREFPRHD\x00\x00\x00\x06" + b"\x00" * 42


class TestAdaptDisplay(unittest.TestCase):
    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-postwrite-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder

    def card(self, *prefs: str) -> tuple[Path, rdb.Rdb]:
        """A drive whose boot volume holds the given saved screen modes."""
        path = self.scratch() / "card.hdf"
        geometry = rdb.Geometry()
        size = 60 * MIB
        table = rdb.Rdb(
            geometry=geometry,
            partitions=[rdb.Partition("DH0", 1, 50, rdb.DOSTYPE_PFS3,
                                      bootable=True)],
            filesystems=[],
            cylinders=(size // 512) // geometry.cyl_blocks)
        with open(path, "wb") as handle:
            handle.truncate(size)
            table.write(handle, 0)
        part = table.partitions[0]
        with open(path, "r+b") as handle:
            writer = pfs3.Pfs3Writer(handle, part.byte_offset(geometry),
                                     part.blocks(geometry), "System")
            writer.format()
            folder = writer.makedirs("Prefs/Env-Archive/Sys")
            for name in prefs:
                writer.write_file(folder, name, PREFS)
            writer.close()
        return path, table

    def read(self, path: Path, table: rdb.Rdb, name: str) -> bytes:
        part = table.partitions[0]
        with open(path, "rb") as handle:
            volume = pfs3.Pfs3Volume(handle, part.byte_offset(table.geometry))
            entry = volume.find(f"Prefs/Env-Archive/Sys/{name}")
            return volume.read_file(entry) if entry else b""

    def adapt(self, path: Path, table: rdb.Rdb, rtg: bool) -> int:
        with open(path, "r+b") as handle:
            return postwrite.adapt_display(handle, 0, table, rtg, QUIET)

    def test_a_saved_rtg_mode_is_cleared_when_there_is_no_rtg_screen(self):
        path, table = self.card("screenmode.prefs.PI")
        self.assertEqual(self.adapt(path, table, rtg=False), 1)
        data = self.read(path, table, "screenmode.prefs.PI")
        self.assertEqual(set(data), {0})

    def test_an_rtg_machine_is_left_alone(self):
        """The saved mode is the right one there; undoing it would be wrong."""
        path, table = self.card("screenmode.prefs.PI")
        self.assertEqual(self.adapt(path, table, rtg=True), 0)
        self.assertEqual(self.read(path, table, "screenmode.prefs.PI"), PREFS)

    def test_only_the_pistorm_preference_is_touched(self):
        """CaffeineOS keeps one per board; the emulator's is not ours to edit."""
        path, table = self.card("screenmode.prefs.PI", "screenmode.prefs.UAE")
        self.adapt(path, table, rtg=False)
        self.assertEqual(set(self.read(path, table, "screenmode.prefs.PI")), {0})
        self.assertEqual(self.read(path, table, "screenmode.prefs.UAE"), PREFS)

    def test_a_system_with_no_saved_mode_is_untouched(self):
        path, table = self.card()
        self.assertEqual(self.adapt(path, table, rtg=False), 0)

    def test_the_size_and_layout_are_not_disturbed(self):
        """Blanking writes over data only - no metadata surgery."""
        path, table = self.card("screenmode.prefs.PI")
        part = table.partitions[0]
        with open(path, "rb") as handle:
            volume = pfs3.Pfs3Volume(handle, part.byte_offset(table.geometry))
            before = volume.find("Prefs/Env-Archive/Sys/screenmode.prefs.PI")
            size, anode = before.size, before.anode
        self.adapt(path, table, rtg=False)
        with open(path, "rb") as handle:
            volume = pfs3.Pfs3Volume(handle, part.byte_offset(table.geometry))
            after = volume.find("Prefs/Env-Archive/Sys/screenmode.prefs.PI")
            self.assertEqual((after.size, after.anode), (size, anode))
            #  The rest of the volume still reads.
            self.assertTrue(volume.find("Prefs/Env-Archive/Sys"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
