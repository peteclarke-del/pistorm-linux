"""What a collection is divided into, and what a given Amiga can run."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import amigainfo, amigaos, content, machines, packages  # noqa: E402


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


class Dependencies(unittest.TestCase):
    """A package that cannot run alone must bring what it needs."""

    def test_mui_applications_pull_mui_in(self):
        for key in ("igame", "netsurf", "amftp", "wookiechat", "ibrowse"):
            with self.subTest(key):
                self.assertIn("mui", packages.expand([key]),
                              f"{key} is a MUI application and would open "
                              f"nothing without muimaster.library")

    def test_a_dependency_comes_before_what_needs_it(self):
        order = packages.expand(["igame"])
        self.assertLess(order.index("mui"), order.index("igame"))

    def test_expand_keeps_what_was_asked_for_and_adds_no_duplicates(self):
        keys = ["igame", "amftp", "netsurf"]
        out = packages.expand(keys)
        self.assertEqual(len(out), len(set(out)))
        for key in keys:
            self.assertIn(key, out)

    def test_mui_assigns_itself_in_user_startup(self):
        #  Copying MUI's files is not enough: it is found through MUI:, and
        #  its libraries only through LIBS: having MUI:Libs added to it.
        lines = "\n".join(packages.CATALOGUE_BY_KEY["mui"].startup)
        self.assertIn("Assign >NIL: MUI: SYS:System/MUI", lines)
        self.assertIn("ADD LIBS: MUI:Libs", lines)

    def test_a_shared_library_is_only_copied_once(self):
        #  Three packages want codesets.library, and the writer refuses to
        #  overwrite a file that is already there.
        donor = Path(tempfile.mkdtemp(prefix="pistorm-donor-"))
        self.addCleanup(shutil.rmtree, donor, True)
        system = donor / "System"
        #  A C drawer is what marks a folder as a system drive.
        (system / "C").mkdir(parents=True)
        (system / "Libs").mkdir(parents=True)
        for name in ("codesets.library", "openurl.library"):
            (system / "Libs" / name).write_bytes(b"x")
        for drawer in ("Internet/WookieChat", "Internet/AWeb_APL",
                       "Internet/IBrowse"):
            (system / drawer).mkdir(parents=True)
        pairs = packages.overlays_for(donor, ["wookiechat", "aweb", "ibrowse"],
                                      allow_download=False)
        self.assertEqual(len(pairs), len(set(pairs)))
        codesets = [d for s, d in pairs if s.endswith("codesets.library")]
        self.assertEqual(len(codesets), 1, "copied more than once")

    def test_support_survives_a_downloaded_payload(self):
        #  NetSurf comes from Aminet, but its supporting libraries still have
        #  to come off the donor.  Putting them in items made the donor look
        #  like the source of NetSurf itself and skipped the download.
        netsurf = packages.CATALOGUE_BY_KEY["netsurf"]
        self.assertEqual(netsurf.items, ())
        self.assertTrue(netsurf.download)
        self.assertTrue(netsurf.support)


class ExcludedDrawerIcons(unittest.TestCase):
    """Leaving a category out must take its icon with it."""

    def test_the_drawers_icon_goes_too(self):
        skip = ["whdload/aga"]
        self.assertTrue(amigaos._excluded("whdload/aga", skip))
        self.assertTrue(amigaos._excluded("whdload/aga/game", skip))
        self.assertTrue(amigaos._excluded("whdload/aga.info", skip),
                        "the icon was left behind, so Workbench shows a "
                        "drawer that is not on the card")

    def test_a_similarly_named_drawer_is_kept(self):
        skip = ["whdload/aga"]
        self.assertFalse(amigaos._excluded("whdload/agatha", skip))
        self.assertFalse(amigaos._excluded("whdload/agatha.info", skip))
        self.assertFalse(amigaos._excluded("whdload/ocs", skip))


class IconPositions(unittest.TestCase):
    """Copied icons must not all claim the same square of the window."""

    def _icon(self, x, y):
        import struct
        raw = bytearray(amigainfo.DISKOBJECT_SIZE)
        struct.pack_into(">II", raw, amigainfo.CURRENT_X, x, y)
        return bytes(raw)

    def test_a_snapshotted_position_is_forgotten(self):
        import struct
        out = amigainfo.clear_position(self._icon(120, 48))
        x, y = struct.unpack_from(">II", out, amigainfo.CURRENT_X)
        self.assertEqual(x, amigainfo.NO_ICON_POSITION)
        self.assertEqual(y, amigainfo.NO_ICON_POSITION)

    def test_nothing_else_in_the_icon_changes(self):
        before = self._icon(10, 20)
        after = amigainfo.clear_position(before)
        self.assertEqual(len(after), len(before))
        self.assertEqual(after[:amigainfo.CURRENT_X],
                         before[:amigainfo.CURRENT_X])
        self.assertEqual(after[amigainfo.CURRENT_Y + 4:],
                         before[amigainfo.CURRENT_Y + 4:])

    def test_something_too_short_to_be_an_icon_is_left_alone(self):
        self.assertEqual(amigainfo.clear_position(b"nope"), b"nope")


class DrawerIconTypes(unittest.TestCase):
    """Only a drawer icon may be given to a drawer."""

    def _icon(self, kind, drawerdata):
        import struct
        raw = bytearray(amigainfo.DISKOBJECT_SIZE)
        struct.pack_into(">H", raw, 0, amigainfo.MAGIC)
        raw[amigainfo.TYPE_OFFSET] = kind
        struct.pack_into(">I", raw, amigainfo.DRAWER_DATA, drawerdata)
        return bytes(raw)

    def test_a_drawer_icon_is_accepted(self):
        self.assertTrue(amigainfo.is_drawer_icon(self._icon(2, 0x1234)))

    def test_a_project_icon_is_refused(self):
        #  This is the one that produced "unable to open script": a drawer
        #  wearing the project icon of somebody's installer.
        self.assertFalse(amigainfo.is_drawer_icon(self._icon(4, 0)))

    def test_a_tool_icon_is_refused(self):
        self.assertFalse(amigainfo.is_drawer_icon(self._icon(3, 0)))

    def test_a_drawer_icon_with_no_drawer_data_is_refused(self):
        self.assertFalse(amigainfo.is_drawer_icon(self._icon(2, 0)))

    def test_something_that_is_not_an_icon_is_refused(self):
        self.assertFalse(amigainfo.is_drawer_icon(b"not an icon at all"))
        self.assertFalse(amigainfo.is_drawer_icon(
            bytes(amigainfo.DISKOBJECT_SIZE)))
