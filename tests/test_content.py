"""What a collection is divided into, and what a given Amiga can run."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import content, machines  # noqa: E402


class TestDiscover(unittest.TestCase):
    def tree(self, container: str, *names: str) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-content-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        for name in names:
            (folder / container / name / "Title").mkdir(parents=True)
        return folder

    def test_categories_come_from_the_tree_not_a_fixed_list(self):
        found = content.discover(self.tree("WHDLOAD", "AGA", "Cracktros",
                                           "SomethingNew"))
        self.assertEqual([c.label for c in found],
                         ["AGA", "Cracktros", "SomethingNew"])

    def test_an_unknown_category_is_offered_with_nothing_assumed(self):
        found = content.discover(self.tree("WHDLOAD", "SomethingNew"))
        self.assertIsNone(found[0].needs)
        a500 = machines.MACHINES_BY_KEY["a500"]
        self.assertTrue(found[0].suits(a500))

    def test_aga_and_cd32_do_not_suit_a_machine_without_aga(self):
        found = content.discover(self.tree("WHDLOAD", "AGA", "CD32", "OCS"))
        a500 = machines.MACHINES_BY_KEY["a500"]
        self.assertEqual(sorted(content.unsuitable(found, a500)),
                         ["WHDLOAD/AGA", "WHDLOAD/CD32"])

    def test_an_aga_machine_can_run_all_of_them(self):
        found = content.discover(self.tree("WHDLOAD", "AGA", "CD32", "OCS"))
        aga = next(m for m in machines.MACHINES if m.aga)
        self.assertEqual(content.unsuitable(found, aga), [])

    def test_cdtv_is_an_ocs_machine_so_it_suits_an_a500(self):
        """Easy to assume a CD category means AGA; CDTV is an A500 with a CD."""
        found = content.discover(self.tree("WHDLOAD", "CDTV"))
        self.assertTrue(found[0].suits(machines.MACHINES_BY_KEY["a500"]))

    def test_the_name_on_disk_is_kept_whatever_its_case(self):
        """The copy matches the real path, so the case has to survive."""
        found = content.discover(self.tree("WHDLOAD", "Cinemaware"))
        self.assertEqual(found[0].path, "WHDLOAD/Cinemaware")

    def test_entries_are_counted_so_the_size_of_a_choice_is_visible(self):
        folder = self.tree("WHDLOAD", "OCS")
        for extra in ("A", "B", "C"):
            (folder / "WHDLOAD" / "OCS" / extra).mkdir()
        self.assertEqual(content.discover(folder)[0].entries, 4)

    def test_a_tree_with_no_categories_says_so_rather_than_guessing(self):
        folder = Path(tempfile.mkdtemp(prefix="pistorm-content-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "SomeGame").mkdir()
        self.assertEqual(content.discover(folder), [])

    def test_a_folder_that_is_not_there(self):
        self.assertEqual(content.discover("/no/such/place"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
