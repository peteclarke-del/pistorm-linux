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


class ResolvedFromTheBinaries(unittest.TestCase):
    """What a program needs is read out of it, not listed by hand.

    Naming dependencies by hand caught MUI and a few libraries and missed
    twenty more, each of which copied onto the card and then would not run.
    """

    def setUp(self):
        self.donor = Path(tempfile.mkdtemp(prefix="pistorm-donor-"))
        self.addCleanup(shutil.rmtree, self.donor, True)
        self.system = self.donor / "System"
        for drawer in ("C", "Libs", "Devs", "Classes", "Internet/Thing"):
            (self.system / drawer).mkdir(parents=True)
        for name in ("bsdsocket.library", "ixemul.library"):
            (self.system / "Libs" / name).write_bytes(b"L" * 4096)
        (self.system / "Devs" / "netinfo.device").write_bytes(b"D" * 4096)

    def _program(self, *mentions):
        #  Real binaries separate their strings with NULs; without one the
        #  padding runs into the first name and the scan reads them as a
        #  single word, which is the over-matching the resolver tolerates.
        body = (b"PADDING\x00" * 400
                + b"".join(m.encode("latin-1") + b"\x00" for m in mentions)
                + b"tail\x00" * 200)
        (self.system / "Internet" / "Thing" / "thing").write_bytes(body)
        return [(str(self.system / "Internet" / "Thing"), "Internet/Thing")]

    def test_a_library_a_program_names_is_found_and_copied(self):
        pairs = self._program("bsdsocket.library", "netinfo.device")
        extra = packages.resolve_dependencies(pairs, self.donor)
        got = {Path(s).name for s, _d in extra}
        self.assertEqual(got, {"bsdsocket.library", "netinfo.device"})

    def test_each_lands_where_the_donor_keeps_it(self):
        extra = packages.resolve_dependencies(
            self._program("bsdsocket.library", "netinfo.device"), self.donor)
        where = {Path(s).name: d for s, d in extra}
        self.assertEqual(where["bsdsocket.library"], "Libs")
        self.assertEqual(where["netinfo.device"], "Devs")

    def test_a_rom_library_is_not_copied(self):
        #  dos.library is in Kickstart; copying one would be worse than
        #  useless, it would shadow the ROM.
        (self.system / "Libs" / "dos.library").write_bytes(b"x" * 4096)
        extra = packages.resolve_dependencies(
            self._program("dos.library", "intuition.library"), self.donor)
        self.assertEqual(extra, [])

    def test_something_already_being_copied_is_not_copied_again(self):
        (self.system / "Internet" / "Thing" / "ixemul.library").write_bytes(
            b"x" * 4096)
        extra = packages.resolve_dependencies(
            self._program("ixemul.library"), self.donor)
        self.assertEqual(extra, [],
                         "it travels inside the drawer already")

    def test_a_name_the_donor_does_not_have_is_simply_dropped(self):
        #  The scan over-matches where two strings abut; a fragment resolves
        #  to nothing and costs nothing.
        extra = packages.resolve_dependencies(
            self._program("nusomething.library"), self.donor)
        self.assertEqual(extra, [])

    def test_no_donor_means_no_guessing(self):
        self.assertEqual(packages.resolve_dependencies(
            self._program("bsdsocket.library"), None), [])

    def test_a_dependency_of_a_dependency_is_found_too(self):
        """One round left seven behind: mmu wants 68030, ixemul wants ixnet."""
        (self.system / "Libs" / "ixnet.library").write_bytes(
            b"PAD\x00" * 400 + b"deeper.library\x00")
        (self.system / "Libs" / "deeper.library").write_bytes(b"z" * 4096)
        #  ixemul names ixnet, which names deeper.  Only ixemul is referenced
        #  by the program itself.
        (self.system / "Libs" / "ixemul.library").write_bytes(
            b"PAD\x00" * 400 + b"ixnet.library\x00")
        extra = packages.resolve_dependencies(
            self._program("ixemul.library"), self.donor)
        got = {Path(s).name for s, _d in extra}
        self.assertEqual(got, {"ixemul.library", "ixnet.library",
                               "deeper.library"})

    def test_resolution_terminates_when_libraries_name_each_other(self):
        #  A pair that reference one another must not loop for ever.
        (self.system / "Libs" / "ping.library").write_bytes(
            b"PAD\x00" * 400 + b"pong.library\x00")
        (self.system / "Libs" / "pong.library").write_bytes(
            b"PAD\x00" * 400 + b"ping.library\x00")
        extra = packages.resolve_dependencies(
            self._program("ping.library"), self.donor)
        got = {Path(s).name for s, _d in extra}
        self.assertEqual(got, {"ping.library", "pong.library"})

    def test_a_key_file_travels_with_what_it_unlocks(self):
        #  Registered software looks for <name>.key beside the system, not in
        #  its own drawer.  Copy xadmaster.library and leave xadmaster.key and
        #  it runs crippled, which reads as the copy having failed.
        (self.system / "S").mkdir(exist_ok=True)
        (self.system / "Libs" / "xadmaster.library").write_bytes(b"x" * 4096)
        (self.system / "S" / "xadmaster.key").write_bytes(b"key")
        extra = packages.resolve_dependencies(
            self._program("xadmaster.library"), self.donor)
        where = {Path(s).name: d for s, d in extra}
        self.assertIn("xadmaster.key", where)
        self.assertEqual(where["xadmaster.key"], "S")

    def test_a_key_for_something_not_being_copied_is_left(self):
        (self.system / "S").mkdir(exist_ok=True)
        (self.system / "S" / "SomethingElse.key").write_bytes(b"key")
        extra = packages.resolve_dependencies(
            self._program("bsdsocket.library"), self.donor)
        self.assertNotIn("SomethingElse.key",
                         {Path(s).name for s, _d in extra})

    def test_no_pair_is_ever_produced_twice(self):
        #  The writer refuses to overwrite, so one duplicate ends a build.
        (self.system / "S").mkdir(exist_ok=True)
        (self.system / "S" / "ixemul.key").write_bytes(b"key")
        pairs = self._program("ixemul.library", "bsdsocket.library")
        extra = packages.resolve_dependencies(pairs, self.donor)
        both = pairs + extra
        self.assertEqual(len(both), len(set(both)))


class SoftKickBeforeIPrefs(unittest.TestCase):
    """The disk icon.library must replace the ROM one, or icons stay blank.

    A modern Amiga icon keeps its picture in an appended OS3.5 colour chunk
    and leaves the classic image 0x0.  Kickstart 3.1's icon.library 40.1
    cannot read that and draws nothing, so a card full of good icons comes up
    with half of them blank.  Soft-kicking the replacement from
    S:User-Startup does not work: IPrefs has already opened the ROM one, and
    a library in use cannot be flushed.  Booted and asked, the Amiga answered
    "icon.library 40.1" while 51.4 sat unused in LIBS:.
    """

    LINES = ["IF EXISTS LIBS:icon.library",
             "   C:LoadResident LIBS:icon.library",
             "EndIF"]

    def _edit(self, body):
        from pistorm_imager.core.util import Progress          # noqa: PLC0415
        editor = amigaos.StartupSequenceEditor(self.LINES, Progress())
        out = editor.offer("S/Startup-Sequence", body.encode("latin-1"))
        return editor, out.decode("latin-1")

    def test_the_soft_kick_goes_in_before_iprefs(self):
        editor, out = self._edit(
            "C:SetPatch QUIET\nBindDrivers\nC:IPrefs\nC:LoadWB\n")
        self.assertTrue(editor.inserted)
        lines = [line.strip() for line in out.splitlines()]
        self.assertLess(lines.index("C:LoadResident LIBS:icon.library"),
                        lines.index("C:IPrefs"))

    def test_everything_that_was_there_is_still_there(self):
        body = "C:SetPatch QUIET\nBindDrivers\nC:IPrefs\nC:LoadWB\n"
        _editor, out = self._edit(body)
        for line in body.splitlines():
            self.assertIn(line, out)

    def test_another_file_is_left_completely_alone(self):
        from pistorm_imager.core.util import Progress          # noqa: PLC0415
        editor = amigaos.StartupSequenceEditor(self.LINES, Progress())
        self.assertEqual(editor.offer("S/User-Startup", b"anything"),
                         b"anything")
        self.assertEqual(editor.offer("C/Copy", b"binary"), b"binary")
        self.assertFalse(editor.inserted)

    def test_a_startup_with_no_iprefs_is_not_mangled(self):
        #  Better to leave it alone and say so than to guess at a place.
        editor, out = self._edit("Echo \"a strange startup\"\n")
        self.assertFalse(editor.inserted)
        self.assertEqual(out, "Echo \"a strange startup\"\n")

    def test_it_is_only_inserted_once(self):
        editor, out = self._edit(
            "C:SetPatch\nC:IPrefs\nC:ConClip\nC:IPrefs\n")
        self.assertEqual(out.count("C:LoadResident LIBS:icon.library"), 1)

class WhdloadNeedsKickstarts(unittest.TestCase):
    """A slave asks WHDLoad for the Kickstart the game expects."""

    def test_whdload_asks_for_the_kickstart_images(self):
        #  These are ROM images, not code: nothing names them inside a binary,
        #  so no scan can find them and they have to be declared.  Without
        #  them iGame launches a game and the machine falls over.
        support = dict(packages.CATALOGUE_BY_KEY["whdload"].support)
        self.assertIn("Devs/Kickstarts", support)
        self.assertEqual(support["Devs/Kickstarts"], "Devs/Kickstarts")


class UpdatesForAnAcceleratedMachine(unittest.TestCase):
    """Workbench 3.1 is not set up for the CPU a PiStorm provides.

    Its SetPatch is 40.16 from 1994, which predates the 68040 and does not
    set one up, and its 68040.library is 37.30.  On a card built from the
    floppies that leaves WHDLoad taking a privilege violation the moment it
    tries to start a game.
    """

    def test_the_cpu_libraries_are_offered_as_an_update(self):
        keys = {p.key for p in
                packages.in_category(packages.Category.UPDATES)}
        self.assertIn("mmulib", keys)
        self.assertIn("setpatch", keys)

    def test_whdload_cannot_be_installed_without_them(self):
        order = packages.expand(["whdload"])
        for key in ("setpatch", "mmulib"):
            self.assertIn(key, order)
            self.assertLess(order.index(key), order.index("whdload"))

    def test_the_cpu_libraries_come_from_aminet_not_a_donor(self):
        #  They are freely distributable, so a card built from floppies alone
        #  can still have them - which is the whole point of offering them.
        mmulib = packages.CATALOGUE_BY_KEY["mmulib"]
        self.assertTrue(mmulib.download)
        self.assertEqual(mmulib.items, ())
        self.assertEqual(dict(mmulib.download.items)["MMULib/Libs"], "Libs")

    def test_setpatch_can_only_come_from_a_donor(self):
        #  Commodore's, from a later release, and not on Aminet.
        setpatch = packages.CATALOGUE_BY_KEY["setpatch"]
        self.assertIsNone(setpatch.download)
        self.assertEqual(dict(setpatch.items)["C/SetPatch"], "C")

    def test_a_suggested_build_updates_the_cpu_support(self):
        from pistorm_imager.core.machines import Chipset, Display  # noqa
        donor = Path(tempfile.mkdtemp(prefix="pistorm-donor-"))
        self.addCleanup(shutil.rmtree, donor, True)
        (donor / "System" / "C").mkdir(parents=True)
        (donor / "System" / "C" / "SetPatch").write_bytes(b"x")
        machine = machines.MACHINES[0]
        chosen = packages.suggested(machine, Display.NATIVE, donor=donor)
        self.assertIn("setpatch", chosen)
