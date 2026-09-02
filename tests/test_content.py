"""What a collection is divided into, and what a given Amiga can run."""
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import (amigainfo, amigaos, compat, content,  # noqa: E402
                                 machines, packages, rdb)
from pistorm_imager.core.util import Progress  # noqa: E402


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
        for name in ("codesets.library", "ixemul.library"):
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
        pairs = self._program("codesets.library", "netinfo.device")
        extra = packages.resolve_dependencies(pairs, self.donor)
        got = {Path(s).name for s, _d in extra}
        self.assertEqual(got, {"codesets.library", "netinfo.device"})

    def test_each_lands_where_the_donor_keeps_it(self):
        extra = packages.resolve_dependencies(
            self._program("codesets.library", "netinfo.device"), self.donor)
        where = {Path(s).name: d for s, d in extra}
        self.assertEqual(where["codesets.library"], "Libs")
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
            self._program("codesets.library"), None), [])

    def test_a_dependency_of_a_dependency_is_found_too(self):
        """One round left seven behind: mmu wants 68030, ixemul wants ixnet."""
        (self.system / "Libs" / "middle.library").write_bytes(
            b"PAD\x00" * 400 + b"deeper.library\x00")
        (self.system / "Libs" / "deeper.library").write_bytes(b"z" * 4096)
        #  ixemul names middle, which names deeper.  Only ixemul is
        #  referenced by the program itself.
        (self.system / "Libs" / "ixemul.library").write_bytes(
            b"PAD\x00" * 400 + b"middle.library\x00")
        extra = packages.resolve_dependencies(
            self._program("ixemul.library"), self.donor)
        got = {Path(s).name for s, _d in extra}
        self.assertEqual(got, {"ixemul.library", "middle.library",
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
            self._program("codesets.library"), self.donor)
        self.assertNotIn("SomethingElse.key",
                         {Path(s).name for s, _d in extra})

    def test_no_pair_is_ever_produced_twice(self):
        #  The writer refuses to overwrite, so one duplicate ends a build.
        (self.system / "S").mkdir(exist_ok=True)
        (self.system / "S" / "ixemul.key").write_bytes(b"key")
        pairs = self._program("ixemul.library", "codesets.library")
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

    def test_whdload_does_not_drag_in_what_stops_it_working(self):
        """These were once required by WHDLoad.  It was exactly backwards.

        Built one variable at a time against a card proven to run a game:
        SetPatch 44.38 alone leaves WHDLoad hanging on a black screen, and
        MMULib alone gives a yellow screen - a CPU exception with no OS left
        to draw a Guru.  Either of them stops every game.
        """
        order = packages.expand(["whdload"])
        for key in ("setpatch", "mmulib"):
            self.assertNotIn(key, order)

    def test_the_cpu_patches_are_off_by_default(self):
        for key in ("setpatch", "mmulib"):
            package = packages.CATALOGUE_BY_KEY[key]
            self.assertFalse(package.default, key)
            self.assertIn("games", package.note.lower(),
                          f"{key} must say what it costs")

    def test_a_suggested_build_leaves_them_out(self):
        from pistorm_imager.core.machines import Display      # noqa: PLC0415
        chosen = packages.suggested(machines.MACHINES[0], Display.NATIVE)
        for key in ("setpatch", "mmulib"):
            self.assertNotIn(key, chosen)

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

    def test_they_are_still_offered_for_a_machine_used_for_applications(self):
        #  Off by default is not the same as gone: the newer CPU support is
        #  a real improvement where WHDLoad is not the point.
        keys = {p.key for p in
                packages.in_category(packages.Category.UPDATES)}
        self.assertEqual(keys, {"mmulib", "setpatch"})


class NiceToHaves(unittest.TestCase):
    """The extras that make a stock Workbench pleasant to use."""

    def test_every_package_has_a_route_onto_the_card(self):
        #  A catalogue entry with neither a donor path nor a download can
        #  never be installed, and would sit in the list doing nothing.
        for package in packages.CATALOGUE:
            with self.subTest(package.key):
                self.assertTrue(package.items or package.download,
                                f"{package.key} has no way of being obtained")

    def test_the_desktop_extras_are_on_by_default(self):
        #  DefIcons is most of why a stock 3.1 desktop looks bare, and a
        #  missing mouse wheel is the first thing anyone notices.
        for key in ("deficons", "freewheel"):
            self.assertTrue(packages.CATALOGUE_BY_KEY[key].default, key)

    def test_the_commodities_land_in_wbstartup(self):
        #  They have to be started at boot to do anything at all.
        for key in ("deficons", "freewheel", "clicktofront"):
            package = packages.CATALOGUE_BY_KEY[key]
            places = list(package.items) + list(
                package.download.items if package.download else ())
            self.assertTrue(any(d == "WBStartup" for _s, d in places),
                            f"{key} never reaches WBStartup")

    def test_media_and_extras_are_offered_as_their_own_groups(self):
        for category, expected in ((packages.Category.MEDIA,
                                    {"amplifier", "hippoplayer",
                                     "digibooster"}),
                                   (packages.Category.EXTRAS,
                                    {"dockit", "visage", "snoopdos",
                                     "diropus4"})):
            keys = {p.key for p in packages.in_category(category)}
            self.assertEqual(keys, expected)

    def test_what_is_not_freely_distributable_needs_a_donor(self):
        #  HippoPlayer and Directory Opus are not on Aminet; offering to
        #  download them would be a promise that cannot be kept.
        for key in ("hippoplayer", "diropus4"):
            package = packages.CATALOGUE_BY_KEY[key]
            self.assertIsNone(package.download, key)
            self.assertTrue(package.items, key)

    def test_no_two_packages_share_a_key(self):
        keys = [p.key for p in packages.CATALOGUE]
        self.assertEqual(len(keys), len(set(keys)))


class NeverScavengeCpuLibraries(unittest.TestCase):
    """The dependency scan must not take CPU support from a donor.

    It did, and it quietly undid a deliberate choice: with the CPU patch
    packages deselected, the scan still copied mmu.library, 68030.library and
    68040.library off the donor because something in the tree named them - and
    those are exactly what stops every WHDLoad game from starting.  Cards were
    built again and again with the packages removed and the libraries still
    there, which sent the search off in entirely the wrong direction.
    """

    def setUp(self):
        self.donor = Path(tempfile.mkdtemp(prefix="pistorm-donor-"))
        self.addCleanup(shutil.rmtree, self.donor, True)
        self.system = self.donor / "System"
        for drawer in ("C", "Libs", "Programs/Thing"):
            (self.system / drawer).mkdir(parents=True)
        for name in ("mmu.library", "68040.library", "68030.library",
                     "680x0.library", "memory.library", "codesets.library"):
            (self.system / "Libs" / name).write_bytes(b"L" * 4096)

    def _program(self, *mentions):
        body = (b"PAD\x00" * 400
                + b"".join(m.encode("latin-1") + b"\x00" for m in mentions))
        (self.system / "Programs" / "Thing" / "thing").write_bytes(body)
        return [(str(self.system / "Programs" / "Thing"), "Programs/Thing")]

    def test_networking_brings_the_stack_not_the_socket_stub(self):
        """The stack publishes the socket library; the card never carries it.

        MiamiDx creates bsdsocket.library in memory once it is online, so a
        networked card needs no copy in LIBS: - and the copy the donor has is
        an AmiTCP stub with no stack behind it that stops every WHDLoad game.
        A card can have both networking and games because of this.
        """
        (self.system / "Libs" / "bsdsocket.library").write_bytes(b"S" * 4096)
        (self.system / "Libs" / "miamipcap.library").write_bytes(b"M" * 4096)
        (self.system / "Internet" / "MiamiDx").mkdir(parents=True, exist_ok=True)
        (self.system / "Internet" / "MiamiDx" / "MiamiDx").write_bytes(b"M" * 64)
        chosen = packages.overlays_for(str(self.donor), ["network"],
                                       allow_download=False)
        names = {Path(s).name for s, _d in chosen}
        self.assertIn("MiamiDx", names)
        self.assertNotIn("bsdsocket.library", names)

    def test_stack_provided_libraries_are_never_taken(self):
        """bsdsocket is put in LIBS: by a running TCP/IP stack.

        Copied as a file it is a stub with nothing behind it, and it stops
        every WHDLoad game: yellow screen, then nothing.  Bisected to this
        one library, alone, against a card proven to run the game.
        """
        for name in ("bsdsocket.library", "usergroup.library",
                     "ixnet.library"):
            (self.system / "Libs" / name).write_bytes(b"S" * 4096)
        extra = packages.resolve_dependencies(
            self._program("bsdsocket.library", "usergroup.library",
                          "ixnet.library"), self.donor)
        self.assertEqual(extra, [],
                         f"scavenged: {[Path(s).name for s, _ in extra]}")

    def test_cpu_libraries_are_never_taken(self):
        pairs = self._program("mmu.library", "68040.library",
                              "68030.library", "680x0.library",
                              "memory.library")
        extra = packages.resolve_dependencies(pairs, self.donor)
        self.assertEqual(extra, [], f"scavenged: {[Path(s).name for s, _ in extra]}")

    def test_ordinary_libraries_are_still_taken(self):
        extra = packages.resolve_dependencies(
            self._program("codesets.library"), self.donor)
        self.assertEqual({Path(s).name for s, _d in extra},
                         {"codesets.library"})

    def test_the_mmulib_package_can_still_install_them_deliberately(self):
        #  Off by default and warned about, but not unreachable.
        mmulib = packages.CATALOGUE_BY_KEY["mmulib"]
        self.assertEqual(dict(mmulib.download.items)["MMULib/Libs"], "Libs")


class _Recorder(Progress):
    """A progress sink that keeps what it was told, so a test can read it."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def log(self, text: str = "") -> None:
        self.lines.append(text)

    def step(self, text: str = "") -> None:
        self.lines.append(text)


class ANetworkStackThatCanBeInstalled(unittest.TestCase):
    """Roadshow: an archive no code can fetch, laid out by drawer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"
        self.cache.mkdir()
        self.addCleanup(self.tmp.cleanup)
        patch = unittest.mock.patch.object(packages, "cache_dir",
                                           lambda: self.cache)
        patch.start()
        self.addCleanup(patch.stop)
        self.package = packages.CATALOGUE_BY_KEY["roadshow"]

    def test_a_missing_archive_says_where_to_get_it(self):
        """Silence would leave a card with no stack and no explanation."""
        log = _Recorder()
        self.assertIsNone(packages.download_archive(self.package, log))
        said = " ".join(log.lines)
        self.assertIn("Roadshow-Demo-1.15.lha", said)
        self.assertIn("roadshow.apc-tcp.de", said)
        self.assertIn(str(self.cache), said)

    def test_nothing_is_downloaded_for_a_manual_package(self):
        """The publisher serves a web page to anything but a browser, and
        caching that as though it were the archive would be worse than
        having no stack at all."""
        with unittest.mock.patch.object(packages.urllib.request, "urlopen",
                                        side_effect=AssertionError("fetched")):
            self.assertIsNone(packages.download_archive(self.package,
                                                        _Recorder()))

    def test_a_cached_archive_is_used(self):
        (self.cache / "Roadshow-Demo-1.15.lha").write_bytes(b"x" * 32)
        found = packages.download_archive(self.package, _Recorder())
        self.assertEqual(found, self.cache / "Roadshow-Demo-1.15.lha")

    def test_the_archive_is_laid_out_by_drawer(self):
        """Placed by drawer name, because this code cannot read the archive
        in advance to learn where each file belongs."""
        root = Path(self.tmp.name) / "unpacked" / "Roadshow"
        for drawer in ("C", "Libs", "Devs/NetInterfaces", "S"):
            (root / drawer).mkdir(parents=True)
        (root / "C" / "AddNetInterface").write_bytes(b"x")
        (root / "Libs" / "bsdsocket.library").write_bytes(b"x")
        (root / "Roadshow.guide").write_bytes(b"x")
        (root / "Install-Roadshow").write_bytes(b"x")
        log = _Recorder()
        pairs = packages._merged(self.package, root.parent, log)
        placed = {Path(s).name: d for s, d in pairs}
        self.assertEqual(placed["C"], "C")
        self.assertEqual(placed["Libs"], "Libs")
        self.assertEqual(placed["Devs"], "Devs")
        #  Nothing is dropped: the documentation and the publisher's own
        #  installer are staged where they can be found, and reported.
        self.assertEqual(placed["Roadshow.guide"], "Storage/Install/Roadshow")
        self.assertEqual(placed["Install-Roadshow"], "Storage/Install/Roadshow")
        self.assertIn("staged", " ".join(log.lines))


class RoadshowsRealLayout(unittest.TestCase):
    """The archive is an installer distribution, not a Workbench disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        self.cache.mkdir()
        patch = unittest.mock.patch.object(packages, "cache_dir",
                                           lambda: self.cache)
        patch.start()
        self.addCleanup(patch.stop)
        self.package = packages.CATALOGUE_BY_KEY["roadshow"]
        #  As it really unpacks: one drawer, its icon beside it, and the
        #  system-shaped part another level down.
        self.root = Path(self.tmp.name) / "unpacked"
        dist = self.root / "Roadshow-Demo-1.15"
        (self.root / "Roadshow-Demo-1.15.info").parent.mkdir(parents=True,
                                                             exist_ok=True)
        for drawer in ("Workbench/C", "Workbench/Libs", "Workbench/S",
                       "Workbench/Devs/Internet", "Workbench/Storage"):
            (dist / drawer).mkdir(parents=True)
        (self.root / "Roadshow-Demo-1.15.info").write_bytes(b"i")
        (dist / "Workbench/Libs/bsdsocket.library").write_bytes(b"x")
        (dist / "Workbench/C/NetShutdown").write_bytes(b"x")
        (dist / "Workbench/S/Network-Startup").write_bytes(b"x")
        (dist / "Workbench/S/User-Startup").write_bytes(b";BEGIN Roadshow")
        (dist / "Install_Roadshow").write_bytes(b"x")
        (dist / "Documentation").mkdir()

    def _placed(self):
        return packages._merged(self.package, self.root, _Recorder())

    def test_the_system_drawers_are_found_below_the_installer(self):
        placed = {Path(s).name: d for s, d in self._placed()}
        self.assertEqual(placed["C"], "C")
        self.assertEqual(placed["Libs"], "Libs")
        self.assertEqual(placed["Devs"], "Devs")

    def test_the_socket_library_is_the_one_from_the_stack(self):
        """The whole point: Roadshow's own, not the donor's dead stub."""
        libs = [s for s, d in self._placed() if d == "Libs"]
        self.assertTrue(any((Path(s) / "bsdsocket.library").exists()
                            or Path(s).name == "bsdsocket.library"
                            for s in libs), libs)

    def test_the_cards_user_startup_is_never_replaced(self):
        """Roadshow's copy is four lines meant to be appended to the card's.
        Placing it as a file would either overwrite everything the build
        wrote there or be skipped, leaving the stack unstarted."""
        placed = self._placed()
        names = [Path(s).name for s, d in placed if d == "S"]
        self.assertIn("Network-Startup", names)
        self.assertNotIn("User-Startup", names)
        #  The lines are added the way every other package adds them.
        self.assertIn("   Execute S:Network-Startup", self.package.startup)

    def test_an_interface_for_this_machine_is_written(self):
        """Every template in the archive is for other people's hardware."""
        written = [s for s, d in self._placed() if d == "Devs/NetInterfaces"]
        self.assertEqual(len(written), 1)
        text = Path(written[0]).read_text()
        self.assertIn("device=vlink.device", text)
        self.assertIn("configure=dhcp", text)

    def test_the_installer_and_docs_are_staged_once(self):
        staged = [Path(s).name for s, d in self._placed()
                  if d.startswith("Storage/Install")]
        self.assertIn("Install_Roadshow", staged)
        self.assertIn("Documentation", staged)
        #  The Workbench drawer is placed, so it must not be staged as well.
        self.assertNotIn("Workbench", staged)


class FinishRunsOncePerBuild(unittest.TestCase):
    """finish() is called for every tree a build copies, and a build copies
    many. Writing the same file twice does not warn - it ends the build."""

    class _Target:
        def __init__(self):
            self.written: list[str] = []

        def makedirs(self, path):
            return path

        def write_file(self, parent, name, data, *, protect=0,
                       check_existing=True, **kw):
            where = f"{parent}/{name}"
            if check_existing and where in self.written:
                raise RuntimeError(f"{name} already exists")
            self.written.append(where)

    def _fixer(self):
        fixer = compat.Compatibility(Progress(), enabled=True)
        fixer.native = True
        fixer.rtg = True
        fixer._rtg_screenmode = None
        fixer._stored_monitors = {}
        return fixer

    def test_the_display_scripts_are_written_once(self):
        fixer, target = self._fixer(), self._Target()
        for _ in range(4):                       # as many trees as a card has
            fixer.finish(target, Progress())
        self.assertEqual(
            [w for w in target.written if "PiStorm-Use-HDMI" in w],
            ["S/PiStorm-Use-HDMI"])


class TheFfsWarningIsAboutFfs(unittest.TestCase):
    """A PFS3 volume is meant to be larger than FFS can manage; saying it is
    unsafe sends people shrinking a partition that was right all along."""

    def test_pfs3_is_not_warned_about(self):
        """PFS3's flag byte reads as FFS, so the family has to be checked."""
        from pistorm_imager.core import amigafs
        pfs3 = rdb.parse_dostype("PFS3")
        self.assertTrue(amigafs.is_ffs(pfs3))
        self.assertFalse(amigafs.is_dos_family(pfs3))

    def test_ffs_still_is(self):
        from pistorm_imager.core import amigafs
        self.assertTrue(amigafs.is_dos_family(amigafs.DOSTYPE_FFS_INTL))
        self.assertTrue(amigafs.is_ffs(amigafs.DOSTYPE_FFS_INTL))


class TheGraphicsPassSeesTheWholeCard(unittest.TestCase):
    """It used to decide after the first tree copied, and by path only."""

    def _fixer(self, **kw):
        fixer = compat.Compatibility(Progress(), enabled=True, **kw)
        return fixer

    def test_it_no_longer_finishes_after_every_tree(self):
        """A volume is filled from many trees; deciding after the first meant
        deciding before the packages were on it at all."""
        self.assertFalse(compat.Compatibility.finish_with_each_tree)

    def test_a_chosen_package_counts_as_an_install(self):
        """A package overlay is copied *to* Libs/Picasso96, so the paths the
        pass is offered never name Picasso96 and it saw nothing."""
        fixer = self._fixer()
        self.assertFalse(fixer._picasso_installed)
        fixer.expect_picasso()
        self.assertTrue(fixer._picasso_installed)

    def test_the_builder_declares_it_from_the_chosen_packages(self):
        from pistorm_imager.core import builder
        config = builder.BuildConfig(target="/tmp/x.img",
                                     package_keys=["picasso96", "whdload"])
        self.assertTrue(builder._make_fixer(config, Progress())
                        ._picasso_installed)
        without = builder.BuildConfig(target="/tmp/x.img",
                                      package_keys=["whdload"])
        self.assertFalse(builder._make_fixer(without, Progress())
                         ._picasso_installed)


class RulesAreAboutWhereAFileLands(unittest.TestCase):
    """A rule naming Storage or Picasso96 has to match the card's path.

    The floppies and the packages are copied into a destination, and the path
    offered to the compatibility pass used to be relative to the source, so
    the Storage floppy's Monitors/PAL never looked like Storage/Monitors/PAL
    and no rule about Storage ever fired.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "storage-floppy"
        (self.source / "Monitors").mkdir(parents=True)
        (self.source / "Monitors" / "PAL").write_bytes(b"PAL" * 64)
        (self.source / "Monitors" / "PAL.info").write_bytes(b"i" * 32)

    def test_a_stored_monitor_is_recognised_through_its_destination(self):
        fixer = compat.Compatibility(Progress(), enabled=True, native=True)
        for name in ("PAL", "PAL.info"):
            data = (self.source / "Monitors" / name).read_bytes()
            fixer.offer(f"Storage/Monitors/{name}", data)
        self.assertIn("pal", fixer._stored_monitors)

    def test_the_source_path_alone_is_not_enough(self):
        """Which is what the copy used to pass, and why nothing matched."""
        fixer = compat.Compatibility(Progress(), enabled=True, native=True)
        fixer.offer("Monitors/PAL", b"PAL" * 64)
        self.assertEqual(fixer._stored_monitors, {})


class StagingIsNotInstalling(unittest.TestCase):
    """A copy waiting in Storage/Install has not been installed."""

    def test_the_installed_driver_counts(self):
        fixer = compat.Compatibility(Progress(), enabled=True, rtg=True)
        fixer.offer("Libs/Picasso96/rtg.library", b"x")
        self.assertTrue(fixer._picasso_installed)

    def test_the_staged_archive_does_not(self):
        fixer = compat.Compatibility(Progress(), enabled=True, rtg=True)
        fixer.offer("Storage/Install/Picasso96/Install-Picasso96", b"x")
        self.assertFalse(fixer._picasso_installed)

    def test_a_chosen_package_still_does(self):
        """Which is how a build knows before anything has been copied."""
        fixer = compat.Compatibility(Progress(), enabled=True, rtg=True)
        fixer.expect_picasso()
        self.assertTrue(fixer._picasso_installed)


class TwoWaysToFillOneDrive(unittest.TestCase):
    """Installing from floppies and filling from a folder both target the
    bootable drive, and the second formats it: the install would vanish."""

    def _config(self, **kw):
        from pistorm_imager.core import builder
        spec = builder.AmigaPartitionSpec(name="DH0", bootable=True,
                                          size=1024 ** 3, **kw)
        return builder.BuildConfig(target="/tmp/card.img", install_amigaos=True,
                                   amiga_partitions=[spec])

    def test_the_clash_is_refused(self):
        problems = self._config(content_folder="/somewhere").validate()
        self.assertTrue(any("Choose one" in p for p in problems), problems)

    def test_a_content_drive_that_does_not_boot_is_fine(self):
        from pistorm_imager.core import builder
        config = builder.BuildConfig(
            target="/tmp/card.img", install_amigaos=True,
            amiga_partitions=[
                builder.AmigaPartitionSpec(name="DH0", bootable=True,
                                           size=1024 ** 3),
                builder.AmigaPartitionSpec(name="DH1", size=1024 ** 3,
                                           content_folder="/somewhere")])
        self.assertFalse([p for p in config.validate() if "Choose one" in p])


class TheLayoutMustFitTheCard(unittest.TestCase):
    """Nothing checked that the drives fit the size asked for, so a layout
    larger than the image was accepted and laid out past the end of it."""

    def _config(self, image_gib, sizes):
        from pistorm_imager.core import builder
        from pistorm_imager.core.util import GIB
        parts = []
        for index, size in enumerate(sizes):
            parts.append(builder.AmigaPartitionSpec(
                name=f"DH{index}", bootable=index == 0,
                size=None if size is None else int(size * GIB)))
        return builder.BuildConfig(target="/tmp/card.img",
                                   image_size=int(image_gib * GIB),
                                   boot_size=512 * 1024 ** 2,
                                   amiga_partitions=parts)

    def _fit_problems(self, config):
        return [p for p in config.validate()
                if "more than the" in p or "too small to be a drive" in p]

    def test_a_layout_larger_than_the_card_is_refused(self):
        problems = self._fit_problems(self._config(16, [10, 30]))
        self.assertTrue(problems, "an over-sized layout was accepted")
        self.assertIn("24.50 GiB more", problems[0])

    def test_the_units_trap_is_explained(self):
        """125G is 125 GiB; a card sold as 125 GB is smaller than that."""
        self.assertIn("'125GB'", self._fit_problems(self._config(16, [10, 30]))[0])

    def test_a_layout_that_fits_is_accepted(self):
        self.assertEqual(self._fit_problems(self._config(64, [10, 20, 10])), [])

    def test_the_boot_partition_counts(self):
        """A card exactly filled by its drives has no room for the boot
        partition, which is where Emu68 itself lives."""
        self.assertTrue(self._fit_problems(self._config(16, [16])))

    def test_no_room_left_for_the_flexible_drive(self):
        problems = self._fit_problems(self._config(16, [15.49, None]))
        self.assertTrue(problems)
        self.assertIn("too small to be a drive", problems[0])


class NoMonitorIsInventedForACard(unittest.TestCase):
    """Renaming the emulator's Picasso96 monitor made a card that would not
    boot - a software error in VideoCore, which is that monitor bringing the
    board up against the rtg.library the donor happens to carry. The driver
    goes on the card; the monitor is left to Picasso96's own installer."""

    def test_the_package_supplies_no_monitor(self):
        package = packages.CATALOGUE_BY_KEY["picasso96"]
        taken = [source for source, _dest in package.support]
        self.assertEqual([t for t in taken if "Monitors" in t], [],
                         f"a monitor is being supplied again: {taken}")

    def test_the_note_says_where_the_monitor_comes_from(self):
        package = packages.CATALOGUE_BY_KEY["picasso96"]
        self.assertIn("Installer", package.note)

    def test_an_adapted_system_still_has_its_monitor_retargeted(self):
        """A system that already had one is a different case: it is being
        moved to this board, not given a board it never had."""
        fixer = compat.Compatibility(Progress(), enabled=True, rtg=True)
        fixer.offer("Devs/Monitors/uaegfx", b"monitor")
        self.assertTrue(fixer.skip("Devs/Monitors/uaegfx"))
        self.assertEqual(fixer.monitor_file, b"monitor")


class TheTrapdoorSwitchReachesTheCard(unittest.TestCase):
    """The switch and the cmdline option are one fact, not two.

    ``move_slow_to_chip`` is what turns a 512K trapdoor expansion into the
    second half of a megabyte of chip RAM. It used to reach cmdline.txt only
    when the quick setup was applied, so a setup loaded with the switch on and
    the option missing built a card without it - and the switch on screen
    still said it was on.
    """

    def test_the_machine_option_is_added_and_removed_by_the_switch(self):
        from pistorm_imager.ui.window import merge_cmdline
        self.assertEqual(merge_cmdline("move_slow_to_chip", ""),
                         "move_slow_to_chip")
        #  Turned off, it goes; anything typed by hand stays.
        self.assertEqual(merge_cmdline("", "move_slow_to_chip sd.verbose=1"),
                         "sd.verbose=1")
        self.assertEqual(
            merge_cmdline("move_slow_to_chip", "sd.verbose=1").split(),
            ["move_slow_to_chip", "sd.verbose=1"])

    def test_a_machine_with_no_trapdoor_never_gets_it(self):
        from pistorm_imager.core import machines
        for machine in machines.MACHINES:
            options = machines.boot_options(machine, machines.Display.NATIVE,
                                            trapdoor_to_chip=True)
            if not machine.trapdoor_ram:
                self.assertNotIn("move_slow_to_chip", options.extra_cmdline,
                                 f"{machine.key} has no trapdoor RAM")

    def test_the_ecs_a500_does_get_it(self):
        from pistorm_imager.core import machines
        machine = next(m for m in machines.MACHINES if m.key == "a500ecs")
        options = machines.boot_options(machine, machines.Display.NATIVE,
                                        trapdoor_to_chip=True)
        self.assertIn("move_slow_to_chip", options.extra_cmdline)


class MagicWbShipsNoInstaller(unittest.TestCase):
    """Its Installer edits S:User-Startup and stopped a machine booting."""

    def test_the_installer_is_not_staged(self):
        package = packages.CATALOGUE_BY_KEY["magicwb"]
        destinations = [dest for _src, dest in package.download.items]
        self.assertFalse([d for d in destinations if "Install" in d],
                         f"the Installer is being staged again: {destinations}")

    def test_the_fonts_and_patterns_are_still_installed(self):
        package = packages.CATALOGUE_BY_KEY["magicwb"]
        destinations = {dest for _src, dest in package.download.items}
        self.assertIn("Fonts", destinations)
        self.assertIn("Prefs/Presets", destinations)

    def test_the_note_says_why(self):
        self.assertIn("booting", packages.CATALOGUE_BY_KEY["magicwb"].note)


class IgameIsInstalledStandalone(unittest.TestCase):
    """Nothing from a donor. A donor's copy is whatever its author installed
    - PiMiga's is v2.1 from 2022 - and it arrives with that person's games
    list, screenshots and settings, all written against their machine. The
    release from Aminet is the whole package and starts empty, which is what
    a program that scans your own drives should do.
    """

    def setUp(self):
        self.package = packages.CATALOGUE_BY_KEY["igame"]

    def test_nothing_is_taken_from_a_donor(self):
        self.assertEqual(self.package.items, ())

    def test_it_comes_from_its_own_release(self):
        self.assertIsNotNone(self.package.download)
        self.assertIn("iGame", self.package.download.path)

    def test_the_processor_matched_build_is_the_one_the_icon_launches(self):
        """Emu68 gives a PiStorm a 68040, one binary per processor is
        shipped, and the icon launches whatever is called plain "iGame"."""
        renames = {name: (inside, dest)
                   for inside, dest, name in self.package.download.rename}
        self.assertIn("iGame", renames)
        inside, dest = renames["iGame"]
        self.assertTrue(inside.endswith(".040"), inside)
        self.assertEqual(dest, "Programs/iGame")

    def test_it_still_needs_mui(self):
        """Its list and its classes are MUI ones, which no download here
        supplies: NList, NListview, Guigfx and TextEditor."""
        self.assertIn("mui", self.package.requires)


class ABareDriveHasNoBootPartition(unittest.TestCase):
    """The whole file is the Amiga drive, so there is nothing to size."""

    def _config(self, **kw):
        from pistorm_imager.core import builder
        from pistorm_imager.core.util import GIB
        return builder.BuildConfig(
            target="/tmp/x.hdf", image_size=2 * GIB, boot_size=0,
            amiga_partitions=[builder.AmigaPartitionSpec(
                name="DH0", bootable=True, size=None)], **kw)

    def test_a_bare_drive_is_not_asked_for_one(self):
        problems = self._config(output_hdf=True).validate()
        self.assertFalse([p for p in problems if "boot partition must" in p])

    def test_a_card_still_is(self):
        problems = self._config(output_hdf=False).validate()
        self.assertTrue([p for p in problems if "boot partition must" in p])


class TheNewestReleaseWins(unittest.TestCase):
    """A published release is the newest there is; a donor's copy is whatever
    its author installed. The release goes on first and the donor fills in
    what the archive does not carry."""

    def test_a_truncated_download_is_not_kept(self):
        """It is still a file, and caching it means every build afterwards
        fails to unpack an archive that looks like it is already there."""
        import io
        import urllib.request
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = Path(tmp.name)
        package = packages.CATALOGUE_BY_KEY["newinstaller"]

        class Short(io.BytesIO):
            headers = {"Content-Length": "1000"}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with unittest.mock.patch.object(packages, "cache_dir", lambda: cache), \
                unittest.mock.patch.object(urllib.request, "urlopen",
                                           lambda *a, **k: Short(b"x" * 10)):
            log = _Recorder()
            self.assertIsNone(packages.download_archive(package, log))
            self.assertIn("stopped early", " ".join(log.lines))
        self.assertEqual(list(cache.glob("*.lha")), [])

    def test_newinstaller_is_offered(self):
        package = packages.CATALOGUE_BY_KEY["newinstaller"]
        self.assertIsNotNone(package.download)
        places = {dest for _src, dest in package.download.items}
        self.assertIn("C", places)
        self.assertIn("Libs", places)

    def test_where_a_download_comes_from_is_named(self):
        self.assertEqual(
            packages.CATALOGUE_BY_KEY["newinstaller"].download.where, "Aminet")
