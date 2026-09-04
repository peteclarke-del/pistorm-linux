"""What a collection is divided into, and what a given Amiga can run."""
import dataclasses
import os
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

    def test_a_program_beside_the_collection_can_be_left_out(self):
        """These were offered nowhere, so a drive could only be taken whole.

        A Games drive keeps its WHDLoad collection in one drawer and forty
        native titles beside it. Only the collection was divided into things
        that could be excluded, so the rest went onto an ECS machine whatever
        they needed.
        """
        folder = Path(tempfile.mkdtemp(prefix="pistorm-content-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "SomeGame").mkdir()
        found = content.discover(folder)
        self.assertEqual([c.path for c in found], ["SomeGame"])

    def test_a_title_that_names_its_own_chipset_is_judged_by_it(self):
        folder = Path(tempfile.mkdtemp(prefix="pistorm-content-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        for name in ("Turrican2AGA", "DeepCoreCD32", "Saga", "Doom"):
            (folder / name).mkdir()
        needs = {c.label: c.needs for c in content.discover(folder)}
        self.assertEqual(needs["Turrican2AGA"], machines.Chipset.AGA)
        self.assertEqual(needs["DeepCoreCD32"], machines.Chipset.AGA)
        #  "Saga" ends in the same three letters and means nothing by them.
        self.assertIsNone(needs["Saga"])
        self.assertIsNone(needs["Doom"])

    def test_the_ones_this_machine_cannot_run_start_switched_off(self):
        folder = Path(tempfile.mkdtemp(prefix="pistorm-content-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        for name in ("Turrican2AGA", "Doom"):
            (folder / name).mkdir()
        found = content.discover(folder)
        a500 = machines.MACHINES_BY_KEY["a500ecs"]
        self.assertEqual(content.unsuitable(found, a500), ["Turrican2AGA"])
        a1200 = next(m for m in machines.MACHINES if m.aga)
        self.assertEqual(content.unsuitable(found, a1200), [])

    def test_hidden_drawers_are_not_offered(self):
        folder = Path(tempfile.mkdtemp(prefix="pistorm-content-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / ".backdrop").mkdir()
        (folder / "Real").mkdir()
        self.assertEqual([c.path for c in content.discover(folder)], ["Real"])

    def test_a_loose_file_is_offered_when_its_name_says_what_it_needs(self):
        """Turrican2AGA on a real drive is a launcher, not a drawer.

        A rule about drawers alone missed the one title on the whole drive
        that could be identified, while listing every file would have buried
        it in save files and icons.
        """
        folder = Path(tempfile.mkdtemp(prefix="pistorm-content-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "Turrican2AGA").write_bytes(b"launcher")
        (folder / "Turrican2AGA.info").write_bytes(b"icon")
        (folder / "T2SavedData.dat").write_bytes(b"save")
        found = {c.path for c in content.discover(folder)}
        self.assertEqual(found, {"Turrican2AGA"})

    def test_an_image_offers_the_same_choices_as_a_folder(self):
        """A drive imported from an .hdf was offered nothing at all."""
        class FakeEntry:
            def __init__(self, name, is_dir, anode=0):
                self.name, self.is_dir, self.anode, self.size = (
                    name, is_dir, anode, 0)

        class FakeVolume:
            def listdir(self, where=0):
                if where == 0:
                    return [FakeEntry("WHDLOAD", True, 1),
                            FakeEntry("Doom", True, 2),
                            FakeEntry("Turrican2AGA", False),
                            FakeEntry("Turrican2AGA.info", False),
                            FakeEntry(".backdrop", True, 3)]
                if where == 1:
                    return [FakeEntry("AGA", True, 4),
                            FakeEntry("OCS", True, 5)]
                return []

        found = content.discover_volume(FakeVolume())
        self.assertEqual([c.path for c in found],
                         ["WHDLOAD/AGA", "WHDLOAD/OCS", "Doom",
                          "Turrican2AGA"])
        a500 = machines.MACHINES_BY_KEY["a500ecs"]
        self.assertEqual(sorted(content.unsuitable(found, a500)),
                         ["Turrican2AGA", "WHDLOAD/AGA"])

    def test_an_unreadable_image_offers_nothing_rather_than_failing(self):
        class Broken:
            def listdir(self, where=0):
                raise OSError("no")
        self.assertEqual(content.discover_volume(Broken()), [])

    def test_a_launcher_takes_what_it_runs_with_it(self):
        """Turrican2AGA is fourteen bytes reading "AmigaGame.exe".

        Leaving the launcher out and keeping the program it names wastes the
        space the exclusion was for, on something nothing can now reach.
        """
        files = {"Turrican2AGA": b"AmigaGame.exe\n",
                 "AmigaGame.exe": b"\x00\x00\x03\xf3" + b"x" * 900,
                 "Doom": None}
        self.assertEqual(
            content.followed(["Turrican2AGA"], files.get, list(files)),
            ["AmigaGame.exe"])

    def test_a_launcher_that_stays_pins_what_it_runs(self):
        #  A shared engine survives as long as anything still runs it.
        files = {"Turrican2AGA": b"AmigaGame.exe\n",
                 "SomethingElse": b"AmigaGame.exe\n",
                 "AmigaGame.exe": b"\x00\x00\x03\xf3" + b"x" * 900}
        self.assertEqual(
            content.followed(["Turrican2AGA"], files.get, list(files)), [])

    def test_a_choice_of_its_own_is_never_taken_away_behind_your_back(self):
        files = {"Turrican2AGA": b"AmigaGame.exe\n", "AmigaGame.exe": b"prog"}
        self.assertEqual(
            content.followed(["Turrican2AGA"], files.get, list(files),
                             offered=["AmigaGame.exe"]), [])

    def test_a_program_is_not_mistaken_for_a_launcher(self):
        #  Amiga executables start with a hunk header, and are not text.
        files = {"Game": b"\x00\x00\x03\xf3\x00\x01", "Data": b"x"}
        self.assertEqual(content.followed(["Game"], files.get, list(files)), [])

    def test_something_too_big_to_be_a_launcher_is_not_followed(self):
        files = {"Readme": b"Data\n" * 400, "Data": b"x"}
        self.assertTrue(len(files["Readme"]) > content.LAUNCHER_LIMIT)
        self.assertEqual(content.followed(["Readme"], files.get, list(files)),
                         [])

    def test_a_name_it_mentions_that_is_not_there_is_ignored(self):
        files = {"Launcher": b"NotOnThisDrive\n"}
        self.assertEqual(content.followed(["Launcher"], files.get,
                                          list(files)), [])

    def test_the_build_really_calls_it_for_a_folder(self):
        """Exercises the builder's own wrapper, not just the rule.

        The rule was tested and passed while the builder used `content`
        without importing it - a NameError that would only have appeared an
        hour into a real build.
        """
        from pistorm_imager.core import builder                # noqa: PLC0415
        folder = Path(tempfile.mkdtemp(prefix="pistorm-follow-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "Turrican2AGA").write_bytes(b"AmigaGame.exe\n")
        (folder / "AmigaGame.exe").write_bytes(b"\x00\x00\x03\xf3" + b"x" * 900)
        (folder / "Doom").mkdir()
        spec = builder.AmigaPartitionSpec("DH1", None, "PFS3", False, 0,
                                          content_folder=str(folder),
                                          exclude=["Turrican2AGA"])
        out = builder._follow_launchers(spec, None, folder, Progress())
        self.assertEqual(sorted(out), ["AmigaGame.exe", "Turrican2AGA"])

    def test_the_build_really_calls_it_for_an_image(self):
        from pistorm_imager.core import builder                # noqa: PLC0415

        class FakeEntry:
            def __init__(self, name, is_dir, data=b""):
                self.name, self.is_dir, self.anode = name, is_dir, 0
                self.size, self.data = len(data), data

        class FakeVolume:
            entries = [FakeEntry("Turrican2AGA", False, b"AmigaGame.exe\n"),
                       FakeEntry("AmigaGame.exe", False, b"\x00\x00\x03\xf3"),
                       FakeEntry("Doom", True)]

            def listdir(self, where=0):
                return self.entries if where == 0 else []

            def read_file(self, entry):
                return entry.data

        spec = builder.AmigaPartitionSpec("DH1", None, "PFS3", False, 0,
                                          content_hdf="/some.hdf",
                                          exclude=["Turrican2AGA"])
        out = builder._follow_launchers(spec, FakeVolume(), None, Progress())
        self.assertEqual(sorted(out), ["AmigaGame.exe", "Turrican2AGA"])

    def test_nothing_excluded_means_nothing_to_follow(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        spec = builder.AmigaPartitionSpec("DH1", None, "PFS3", False, 0)
        self.assertEqual(builder._follow_launchers(spec, None, None,
                                                   Progress()), [])

    def test_a_folder_that_is_not_there(self):
        self.assertEqual(content.discover("/no/such/place"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Dependencies(unittest.TestCase):
    """A package that cannot run alone must bring what it needs."""

    def test_mui_applications_pull_mui_in(self):
        for key in ("igame", "netsurf", "amftp", "wookiechat"):
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
        #  Several packages want codesets.library, and the writer refuses to
        #  overwrite a file that is already there, so a second copy would end
        #  the build rather than merely waste time.
        real = packages.fetch
        packages.fetch = lambda package, progress: [
            ("/nowhere/codesets.library", "Libs"),
            (f"/nowhere/{package.key}", f"Internet/{package.key}")]
        try:
            pairs = packages.overlays_for(["wookiechat", "amftp"],
                                          allow_download=True)
        finally:
            packages.fetch = real
        self.assertEqual(len(pairs), len(set(pairs)))
        codesets = [d for s, d in pairs if s.endswith("codesets.library")]
        self.assertEqual(len(codesets), 1, "copied more than once")


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

    def test_the_card_says_the_images_have_to_be_supplied(self):
        #  These are Commodore ROM images. They used to be copied out of a
        #  donor system; nothing publishes them, so with the donor gone the
        #  only honest thing is to say so where the package is chosen.
        note = packages.CATALOGUE_BY_KEY["whdload"].note.lower()
        self.assertIn("kickstart", note)
        self.assertIn("devs/kickstarts", note)


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

    def test_whdload_does_not_drag_in_what_stops_it_working(self):
        """These were once required by WHDLoad.  It was exactly backwards.

        Built one variable at a time against a card proven to run a game:
        SetPatch 44.38 alone leaves WHDLoad hanging on a black screen, and
        MMULib alone gives a yellow screen - a CPU exception with no OS left
        to draw a Guru.  Either of them stops every game.
        """
        self.assertNotIn("mmulib", packages.expand(["whdload"]))

    def test_the_cpu_patches_are_off_by_default(self):
        package = packages.CATALOGUE_BY_KEY["mmulib"]
        self.assertFalse(package.default)
        self.assertIn("games", package.note.lower(),
                      "mmulib must say what it costs")

    def test_a_suggested_build_leaves_them_out(self):
        from pistorm_imager.core.machines import Display      # noqa: PLC0415
        chosen = packages.suggested(machines.MACHINES[0], Display.NATIVE)
        self.assertNotIn("mmulib", chosen)

    def test_the_cpu_libraries_come_from_aminet_not_a_donor(self):
        #  They are freely distributable, so a card built from floppies alone
        #  can still have them - which is the whole point of offering them.
        mmulib = packages.CATALOGUE_BY_KEY["mmulib"]
        self.assertTrue(mmulib.download)
        self.assertEqual(dict(mmulib.download.items)["MMULib/Libs"], "Libs")

    def test_the_commodore_setpatch_is_not_offered_at_all(self):
        #  It was Commodore's, from a later release, and could only ever come
        #  out of a donor system - and it stopped WHDLoad games starting.
        self.assertNotIn("setpatch", packages.CATALOGUE_BY_KEY)

    def test_they_are_still_offered_for_a_machine_used_for_applications(self):
        #  Off by default is not the same as gone: the newer CPU support is
        #  a real improvement where WHDLoad is not the point.
        keys = {p.key for p in
                packages.in_category(packages.Category.UPDATES)}
        self.assertEqual(keys, {"mmulib"})


class NiceToHaves(unittest.TestCase):
    """The extras that make a stock Workbench pleasant to use."""

    def test_every_package_has_a_route_onto_the_card(self):
        #  A catalogue entry with no source can never be installed, and would
        #  sit in the list doing nothing.
        for package in packages.CATALOGUE:
            with self.subTest(package.key):
                self.assertIsNotNone(
                    package.download,
                    f"{package.key} has no way of being obtained")

    def test_the_desktop_extras_are_on_by_default(self):
        #  DefIcons is most of why a stock 3.1 desktop looks bare, and a
        #  missing mouse wheel is the first thing anyone notices.
        for key in ("deficons", "freewheel"):
            self.assertTrue(packages.CATALOGUE_BY_KEY[key].default, key)

    def test_the_commodities_land_in_wbstartup(self):
        #  They have to be started at boot to do anything at all.
        #
        #  ClickToFront is not among them any more. The archive holds Bryce
        #  Nesbitt's 1987 original, whose only mode is a requester offering
        #  Install or Cancel, so in WBStartup it asked on every boot - read
        #  off a finished card, and reported. It goes to Utilities now.
        #
        #  FreeWheel arrives under a renamed copy rather than a plain item:
        #  the archive's file is FreeWheel_020 and a drive that brings its
        #  own keeps it in WBStartup as FreeWheel, so ours has to land under
        #  that name to take its place instead of running beside it.
        for key in ("deficons", "freewheel", "magicmenu", "fullpalette"):
            package = packages.CATALOGUE_BY_KEY[key]
            places = [d for _s, d in package.download.items]
            places += [d for _s, d, _new in package.download.rename]
            self.assertIn("WBStartup", places,
                          f"{key} never reaches WBStartup")

    def test_media_and_extras_are_offered_as_their_own_groups(self):
        for category, expected in ((packages.Category.MEDIA,
                                    {"amplifier", "hippoplayer",
                                     "digibooster", "ahi"}),
                                   (packages.Category.EXTRAS,
                                    {"dockit", "visage", "snoopdos",
                                     "kingcon", "sysinfo",
                                     "adfdevice"})):
            keys = {p.key for p in packages.in_category(category)}
            self.assertEqual(keys, expected)

    def test_what_cannot_be_fetched_says_where_to_get_it(self):
        #  One publisher serves its archive only to a browser. That is not a
        #  reason to leave the package out, but the person has to be told,
        #  before the build rather than in the log afterwards.
        for package in packages.CATALOGUE:
            if package.download.manual:
                self.assertTrue(package.download.source,
                                f"{package.key} cannot be fetched and does "
                                f"not say where to get it")

    def test_no_two_packages_share_a_key(self):
        keys = [p.key for p in packages.CATALOGUE]
        self.assertEqual(len(keys), len(set(keys)))


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
        pairs = packages._written(self.package, _Recorder())
        written = [s for s, d in pairs if d == "Devs/NetInterfaces"]
        self.assertEqual(len(written), 1)
        text = Path(written[0]).read_text()
        self.assertIn("device=wifipi.device", text)
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


class TheDisksFillWhatAnImportedDriveLacks(unittest.TestCase):
    """A drive can boot and still bring no operating system. ClassicWB's
    carries no C:LoadWB, no C:IPrefs and no workbench.library, because those
    are Commodore's, and its first boot asks for a Workbench disk to copy
    them from. The two used to be refused together, which left no way to
    build that card at all."""

    def _config(self, **kw):
        from pistorm_imager.core import builder
        spec = builder.AmigaPartitionSpec(name="DH0", bootable=True,
                                          size=1024 ** 3,
                                          content_hdf="/somewhere/System.hdf")
        return builder.BuildConfig(target="/tmp/card.img",
                                   amiga_partitions=[spec], **kw)

    def test_a_drive_and_the_disks_together_are_allowed(self):
        problems = self._config(install_amigaos=True,
                                adf_folder="/somewhere").validate()
        self.assertFalse([p for p in problems if "Choose one" in p])

    def test_but_the_disks_have_to_be_somewhere(self):
        problems = self._config(install_amigaos=True, adf_folder="").validate()
        self.assertTrue([p for p in problems
                         if "Workbench disk images" in p], problems)


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


class RTGIsInstalledFromItsOwnArchive(unittest.TestCase):
    """No part of RTG is scavenged from the drive being built on.

    Renaming the *emulator's* Picasso96 monitor made a card that would not
    boot: a software error in VideoCore, which was that monitor bringing the
    board up against the rtg.library the donor happened to carry. The lesson
    was read as "do not supply a monitor", and the result was a feature that
    worked or did not depending on where the drive came from - build on
    ClassicWB, which has no monitor to adapt, and the card got VideoCore.card
    in LIBS: with nothing able to load it.

    The cause was the foreign library, not the monitor. Picasso96 now brings
    its own API library, monitor and settings from its own archive, and the
    two have to travel together: a monitor without the library beside it is
    the original failure.
    """

    def _taken(self):
        package = packages.CATALOGUE_BY_KEY["picasso96"]
        return [source for source, _dest in package.download.items]

    def test_the_monitor_never_travels_without_its_library(self):
        taken = self._taken()
        monitor = [t for t in taken if t.endswith("Devs/Monitors/Picasso96")]
        library = [t for t in taken if t.endswith("Picasso96API.library")]
        self.assertEqual(bool(monitor), bool(library),
                         "a monitor brings the board up, and it must be the "
                         "library shipped with it that receives it - not "
                         f"whatever the drive already had: {taken}")

    def test_everything_it_needs_comes_from_the_archive(self):
        landed = {dest + "/" + Path(src).name
                  for src, dest in packages.CATALOGUE_BY_KEY[
                      "picasso96"].download.items}
        landed |= {dest + "/" + new for _src, dest, new
                   in packages.CATALOGUE_BY_KEY["picasso96"].download.rename}
        for path in ("Libs/Picasso96API.library", "Devs/Monitors/Picasso96",
                     "Devs/Picasso96Settings"):
            self.assertIn(path, landed)

    def test_the_note_says_it_is_installed_not_staged(self):
        package = packages.CATALOGUE_BY_KEY["picasso96"]
        self.assertIn("Installed", package.note)
        self.assertIn("Storage/Install", package.note,
                      "the full archive is still there for the extras")

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


class ChosenSoftwareCanDisplaceWhatIsAlreadyThere(unittest.TestCase):
    """The file system creates files and never overwrites them.

    So whichever copy lands first wins, and the order the build happened to
    run in decided it: a drive imported from an image kept its own years-old
    WHDLoad and the current release the user had ticked was skipped. Which
    copy wins is the user's choice now, and it is made before the drive is
    filled, because afterwards is too late.
    """

    def fixer(self, displace=()):
        from pistorm_imager.core import compat                 # noqa: PLC0415
        c = compat.Compatibility(Progress())
        c.displace(displace)
        return c

    def test_the_package_can_still_write_the_file_it_claimed(self):
        """The whole point, and it was broken end to end.

        Displacing refuses a path while the drive is being filled so the
        package that claimed it can have the name. The package's own files go
        on through the same pass, so leaving it switched on refused those too
        and the file landed nowhere. Whole drawers were unaffected, which is
        what made a card look like a packaging problem rather than this.
        """
        from pistorm_imager.core import amigaos, builder, rdb  # noqa: PLC0415
        folder = Path(tempfile.mkdtemp(prefix="pistorm-claim-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "WHDLoad").write_bytes(b"the current release")

        image = folder / "drive.hdf"
        size = 8 * 1024 * 1024
        with open(image, "wb") as handle:
            handle.truncate(size)
        with open(image, "r+b") as handle:
            volume = amigaos.make_volume(handle, 0, size // 512, "Test",
                                         rdb.DOSTYPE_PFS3)
            fixer = compat.Compatibility(Progress())
            fixer.displace(["C/WHDLoad"])
            #  What the fill would have done: refuse the drive's older copy.
            self.assertTrue(fixer.skip("C/WHDLoad"))
            fixer.stop_displacing()
            spec = builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0,
                overlays=[(str(folder / "WHDLoad"), "C")])
            builder._apply_overlays(volume, spec, fixer, Progress())
            volume.close()

        with open(image, "rb") as handle:
            from pistorm_imager.core import pfs3               # noqa: PLC0415
            back = pfs3.Pfs3Volume(handle, 0)
            entry = back.find("C/WHDLoad")
            self.assertIsNotNone(entry, "the package's own file never landed")
            self.assertEqual(back.read_file(entry), b"the current release")

    def test_a_path_a_package_will_supply_is_refused(self):
        c = self.fixer(["C/WHDLoad"])
        self.assertTrue(c.skip("C/WHDLoad"))

    def test_the_match_ignores_case_as_the_amiga_does(self):
        c = self.fixer(["C/WHDLoad"])
        self.assertTrue(c.skip("c/whdload"))

    def test_everything_else_is_left_alone(self):
        c = self.fixer(["C/WHDLoad"])
        self.assertFalse(c.skip("C/WHDLoadCD32"))
        self.assertFalse(c.skip("Libs/icon.library"))

    def test_it_holds_even_with_the_compatibility_pass_switched_off(self):
        #  This is not a compatibility fix; it is the user's own choice.
        from pistorm_imager.core import compat                 # noqa: PLC0415
        c = compat.Compatibility(Progress(), enabled=False)
        c.displace(["C/WHDLoad"])
        self.assertTrue(c.skip("C/WHDLoad"))

    def test_a_drawer_needs_its_name_free_of_a_file(self):
        #  ClassicWB keeps Visage as a file in Utilities; this build wants a
        #  drawer of that name there, and the collision ended an hour-long
        #  build outright. The copy only ever asks about files, so refusing
        #  the name can never take out a drawer of the same name.
        from pistorm_imager.core import builder                # noqa: PLC0415
        folder = Path(tempfile.mkdtemp(prefix="pistorm-drawer-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "Visage").mkdir()
        paths = builder._landing_paths([(str(folder / "Visage"),
                                         "Utilities/Visage")])
        self.assertEqual(paths, ["Utilities/Visage"])
        self.assertTrue(self.fixer(paths).skip("Utilities/Visage"))

    def test_a_drawer_going_to_the_volume_root_displaces_nothing(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        folder = Path(tempfile.mkdtemp(prefix="pistorm-root-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "Stuff").mkdir()
        self.assertEqual(
            builder._landing_paths([(str(folder / "Stuff"), "")]), [])

    def test_a_drawers_contents_are_never_displaced_one_by_one(self):
        #  A drawer is merged into whatever is there. Only its own name is
        #  claimed - refusing the files inside it would take out the drive's
        #  contents along with them.
        from pistorm_imager.core import builder                # noqa: PLC0415
        folder = Path(tempfile.mkdtemp(prefix="pistorm-land-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "WHDLoad").write_bytes(b"x")
        (folder / "Patterns").mkdir()
        (folder / "Patterns" / "one.iff").write_bytes(b"x")
        paths = builder._landing_paths([(str(folder / "WHDLoad"), "C"),
                                        (str(folder / "Patterns"), "Prefs")])
        self.assertEqual(sorted(paths), ["C/WHDLoad", "Prefs"])
        keeps = self.fixer(paths)
        self.assertFalse(keeps.skip("Prefs/one.iff"),
                         "a file inside the drawer must survive")
        self.assertFalse(keeps.skip("Prefs/Env-Archive/Sys/anything"),
                         "and so must everything else already under it")

    def test_a_card_that_imports_nothing_has_no_clash_to_settle(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        spec = builder.AmigaPartitionSpec("DH0", None, "PFS3", True, 0)
        self.assertFalse(builder._boot_drive_is_filled(
            builder.BuildConfig(target="/tmp/x", amiga_partitions=[spec])))

    def test_a_card_whose_boot_drive_comes_from_an_image_does(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        spec = builder.AmigaPartitionSpec("DH0", None, "PFS3", True, 0,
                                          content_hdf="/some/System.hdf")
        self.assertTrue(builder._boot_drive_is_filled(
            builder.BuildConfig(target="/tmp/x", amiga_partitions=[spec])))

    def test_keeping_the_drives_own_copy_is_the_default_off_switch(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        #  On by default - ticking a package means wanting that release - but
        #  it is a switch, and off means the drive's copy is kept.
        self.assertTrue(builder.BuildConfig(target="/tmp/x")
                        .replace_older_software)


class TheDrivesUserStartupIsKeptAndAddedTo(unittest.TestCase):
    """A drive being imported brings its own S:User-Startup.

    This file system creates files and never overwrites them, so the lines
    the chosen packages need could not be added: the build said
    "S:User-Startup already exists; left alone" and FBlit, FText, Birdie and
    BlazeWCP went onto the card as programs that were never run. Read off a
    finished card, where every one of their lines was absent.
    """

    def test_the_drive_keeps_its_own_file_and_our_lines_follow(self):
        from pistorm_imager.core import amigaos, builder, rdb, pfs3  # noqa: PLC0415
        folder = Path(tempfile.mkdtemp(prefix="pistorm-startup-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        theirs = b";ClassicWB User-Startup\nExecute S:Assign-Startup\n"

        image = folder / "drive.hdf"
        size = 8 * 1024 * 1024
        with open(image, "wb") as handle:
            handle.truncate(size)
        with open(image, "r+b") as handle:
            volume = amigaos.make_volume(handle, 0, size // 512, "Test",
                                         rdb.DOSTYPE_PFS3)
            fixer = compat.Compatibility(Progress())
            fixer.keep_user_startup()
            #  What the copy does when it meets the drive's own file.
            fixer.offer("S/User-Startup", theirs)
            self.assertTrue(fixer.skip("S/User-Startup"),
                            "the drive's copy must be held back")
            self.assertEqual(fixer.kept_user_startup, theirs)
            config = builder.BuildConfig(target="/tmp/x",
                                         package_keys=["fblit"])
            builder._write_user_startup(volume, config, Progress(),
                                        fixer.kept_user_startup)
            volume.close()

        with open(image, "rb") as handle:
            back = pfs3.Pfs3Volume(handle, 0)
            entry = back.find("S/User-Startup")
            self.assertIsNotNone(entry, "no S:User-Startup was written")
            text = back.read_file(entry).decode("latin-1")
        self.assertIn("Execute S:Assign-Startup", text,
                      "the drive's own setup was lost")
        self.assertIn("C:FBlit", text,
                      "the package's line never reached the card")
        self.assertLess(text.index("Assign-Startup"), text.index("C:FBlit"),
                        "ours must come after theirs")

    def test_nothing_is_held_back_when_no_package_needs_a_line(self):
        fixer = compat.Compatibility(Progress())
        fixer.offer("S/User-Startup", b"theirs")
        self.assertFalse(fixer.skip("S/User-Startup"))

    def test_a_card_with_no_drive_to_import_still_gets_its_lines(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        config = builder.BuildConfig(target="/tmp/x", package_keys=["fblit"])
        self.assertIn("C:FBlit >NIL:", builder._package_startup_lines(config))


class TheCacheRemembersWhereItGotThings(unittest.TestCase):
    """Two publishers can serve the same file name.

    Moving WHDLoad from Aminet to its author's site changed nothing at all,
    because both serve "WHDLoad_usr.lha" and the cache is keyed on the name -
    so cards went on being built from a 2007 archive already in the cache
    while the catalogue said 20.0. Found by reading the version string off a
    finished card.
    """

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="pistorm-cache-"))
        self.addCleanup(shutil.rmtree, self.cache, ignore_errors=True)
        patch = unittest.mock.patch.object(packages, "cache_dir",
                                           lambda: self.cache)
        patch.start()
        self.addCleanup(patch.stop)
        self.fetched = []

    def package(self, url, manual=False):
        return packages.Package("x", "X", "d", download=packages.Download(
            url, manual=manual))

    def fake_download(self, url, target):
        """Stand in for the network: record the call, write the file."""
        self.fetched.append(url)
        target.write_bytes(b"from " + url.encode())

    def run_fetch(self, package):
        #  Only the caching decision is under test, not the transfer.
        with unittest.mock.patch.object(
                packages, "_transfer",
                side_effect=self.fake_download, create=True):
            return packages.download_archive(package, Progress())

    def test_a_copy_from_the_same_place_is_reused(self):
        name = self.cache / "Thing.lha"
        name.write_bytes(b"cached")
        name.with_name("Thing.lha.source").write_text(
            "https://example.test/Thing.lha\n")
        got = packages.download_archive(
            self.package("https://example.test/Thing.lha"), Progress())
        self.assertEqual(got.read_bytes(), b"cached")

    def test_a_copy_from_somewhere_else_is_not_reused(self):
        name = self.cache / "Thing.lha"
        name.write_bytes(b"the old one")
        name.with_name("Thing.lha.source").write_text(
            "https://aminet.test/Thing.lha\n")
        #  No network here, so the fetch fails and returns None - but the
        #  point is that it *tried* rather than handing back the old file.
        got = packages.download_archive(
            self.package("https://nowhere.invalid/Thing.lha"), Progress())
        self.assertIsNone(got)

    def test_a_copy_of_unrecorded_origin_is_not_trusted(self):
        (self.cache / "Thing.lha").write_bytes(b"who knows")
        got = packages.download_archive(
            self.package("https://nowhere.invalid/Thing.lha"), Progress())
        self.assertIsNone(got)

    def test_an_archive_supplied_by_hand_is_always_kept(self):
        #  Nothing can fetch these, so provenance is not ours to check and
        #  refusing the cache would leave the package out altogether.
        (self.cache / "Thing.lha").write_bytes(b"put here by the user")
        got = packages.download_archive(
            self.package("https://nowhere.invalid/Thing.lha", manual=True),
            Progress())
        self.assertEqual(got.read_bytes(), b"put here by the user")


class TheUnpackedTreeFollowsTheArchive(unittest.TestCase):
    """The same trap as the archive cache, one level down.

    Fetching a newer archive is no use if what was unpacked from the old one
    is handed back. Every package was correctly re-downloaded and then
    installed from the tree unpacked hours earlier, so a card came out
    carrying WHDLoad 16.8 while the archive sitting beside it was 20.0.
    """

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="pistorm-unpack-"))
        self.addCleanup(shutil.rmtree, self.cache, ignore_errors=True)
        patch = unittest.mock.patch.object(packages, "cache_dir",
                                           lambda: self.cache)
        patch.start()
        self.addCleanup(patch.stop)

    def prepared(self, archive_newer: bool):
        archive = self.cache / "Thing.lha"
        archive.write_bytes(b"an archive")
        tree = self.cache / "Thing.unpacked"
        tree.mkdir()
        (tree / "from-the-old-archive").write_bytes(b"stale")
        stamp = archive.stat().st_mtime
        os.utime(archive, (stamp + 10, stamp + 10) if archive_newer
                 else (stamp - 10, stamp - 10))
        return archive, tree

    def test_a_tree_older_than_its_archive_is_thrown_away(self):
        archive, tree = self.prepared(archive_newer=True)
        packages.unpack(archive, Progress())
        self.assertFalse((tree / "from-the-old-archive").exists(),
                         "the stale tree was handed back again")

    def test_a_tree_newer_than_its_archive_is_kept(self):
        #  The ordinary case: unpacking wrote the tree after the download.
        archive, tree = self.prepared(archive_newer=False)
        got = packages.unpack(archive, Progress())
        self.assertEqual(got, tree)
        self.assertTrue((tree / "from-the-old-archive").exists())


class IgameIsToldWhereTheGamesAre(unittest.TestCase):
    """iGame keeps the drawers it scans in repos.prefs, and ships none.

    Installed cleanly from Aminet it came up with nothing to scan, so "Scan
    Repositories" found nothing and the list stayed empty - on a card whose
    drives were full of games. The build knows which drives it filled.
    """

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="pistorm-repos-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def config(self, partitions, keys=("igame",)):
        from pistorm_imager.core import builder                # noqa: PLC0415
        return builder.BuildConfig(target="/tmp/x.img",
                                   amiga_partitions=partitions,
                                   package_keys=list(keys))

    def part(self, name, volume, folder="", bootable=False):
        from pistorm_imager.core import builder                # noqa: PLC0415
        return builder.AmigaPartitionSpec(name, None, "PFS3", bootable, 0,
                                          content_folder=folder,
                                          volume_name=volume)

    def written(self, config):
        """What the first launcher is told to scan."""
        from pistorm_imager.core import builder                # noqa: PLC0415
        pairs = [p for p in builder._igame_instances(config, Progress(), [])
                 if p[0].endswith("repos.prefs")]
        if not pairs:
            return None
        return Path(pairs[0][0]).read_text()

    def test_the_whdload_drawer_is_named_when_it_is_there(self):
        games = self.folder / "games"
        (games / "WHDLoad").mkdir(parents=True)
        text = self.written(self.config([
            self.part("DH0", "Workbench", bootable=True),
            self.part("DH1", "Games", str(games))]))
        self.assertEqual(text, "Games:WHDLoad\n")

    def test_the_drive_itself_when_there_is_no_whdload_drawer(self):
        demos = self.folder / "demos"
        demos.mkdir(parents=True)
        text = self.written(self.config([
            self.part("DH0", "Workbench", bootable=True),
            self.part("DH1", "Demos", str(demos))]))
        self.assertEqual(text, "Demos:\n")

    def test_a_drive_this_build_did_not_fill_is_not_named(self):
        #  Pointing iGame at a drawer that is not there is exactly what the
        #  donor's own list did, and it is no better written by us.
        text = self.written(self.config([
            self.part("DH0", "Workbench", bootable=True),
            self.part("DH1", "Work")]))
        self.assertIsNone(text)

    def test_nothing_is_written_when_igame_was_not_chosen(self):
        games = self.folder / "g2"
        (games / "WHDLoad").mkdir(parents=True)
        self.assertIsNone(self.written(self.config(
            [self.part("DH1", "Games", str(games))], keys=("whdload",))))

    def test_it_lands_beside_igame(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        games = self.folder / "g3"
        (games / "WHDLOAD").mkdir(parents=True)
        pairs = builder._igame_instances(self.config([
            self.part("DH1", "Games", str(games))]), Progress(), [])
        self.assertEqual(pairs[0][1], "Programs/iGame")
        self.assertTrue(pairs[0][0].endswith("repos.prefs"))


class SoftwareThatNeedsTheBootScriptIsLeftOut(unittest.TestCase):
    """PeterK's icon.library boot-looped a card on real hardware.

    It only works if a line goes into S:Startup-Sequence to soft-kick it over
    the one in ROM. A distribution that carries its own boot script has that
    script written out verbatim, so the line never lands - and the library
    then sits in LIBS: where DefIcons, which asks for icon.library 44, makes
    AmigaOS load it after Workbench has already started on the ROM's v40.
    """

    class Fixer:
        def __init__(self, own_startup):
            self.writes_its_own_startup = own_startup

    def pairs(self):
        return [("/tmp/x/icon.library", "Libs"), ("/tmp/x/WHDLoad", "C")]

    def config(self, keys):
        from pistorm_imager.core import builder                # noqa: PLC0415
        return builder.BuildConfig(target="/tmp/x", package_keys=list(keys))

    def test_it_is_dropped_when_the_drive_brings_its_own_boot_script(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        kept = builder._drop_what_needs_the_boot_script(
            self.pairs(), self.config(["iconlib", "whdload"]),
            self.Fixer(True), Progress())
        self.assertEqual([Path(s).name for s, _d in kept], ["WHDLoad"])

    def test_it_is_installed_when_the_boot_script_is_ours_to_edit(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        kept = builder._drop_what_needs_the_boot_script(
            self.pairs(), self.config(["iconlib", "whdload"]),
            self.Fixer(False), Progress())
        self.assertEqual(len(kept), 2)

    def test_everything_else_is_untouched_either_way(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        kept = builder._drop_what_needs_the_boot_script(
            self.pairs(), self.config(["whdload"]), self.Fixer(True),
            Progress())
        self.assertEqual(len(kept), 2)

    def test_the_warning_names_the_package_and_why(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        said = []

        class Loud(Progress):
            def log(self, message):
                said.append(message)

        builder._drop_what_needs_the_boot_script(
            self.pairs(), self.config(["iconlib"]), self.Fixer(True), Loud())
        joined = " ".join(said)
        self.assertIn("icon.library", joined)
        self.assertIn("S:Startup-Sequence", joined)


class TwoPackagesDoingOneJobAreAlternatives(unittest.TestCase):
    """Ticking a second icon set is rarely what anybody means.

    A role names the job, and two packages sharing one are alternatives that
    patch the same part of Workbench. The catalogue is deliberately sparing
    with these: three module players on one card is a preference, not a
    conflict, and a false clash would nag about a choice that is fine.
    """

    def test_the_two_default_icon_systems_share_a_role(self):
        deficons = packages.CATALOGUE_BY_KEY["deficons"]
        newicons = packages.CATALOGUE_BY_KEY["newicons"]
        self.assertTrue(deficons.role)
        self.assertEqual(deficons.role, newicons.role)

    def test_things_that_happily_coexist_have_no_role(self):
        for key in ("amplifier", "hippoplayer", "digibooster", "whdload",
                    "lha", "netsurf", "snoopdos", "sysinfo"):
            self.assertEqual(packages.CATALOGUE_BY_KEY[key].role, "",
                             f"{key} does not exclude anything")

    def test_a_role_never_names_only_one_package(self):
        """A role with a single member could never raise a question."""
        from collections import Counter                       # noqa: PLC0415
        counted = Counter(p.role for p in packages.CATALOGUE if p.role)
        alone = [role for role, n in counted.items() if n < 2]
        self.assertEqual(alone, [], f"roles with nothing to clash with: {alone}")


class SysInfoIsTheVersionThatSurvivesNoFpu(unittest.TestCase):
    """SysInfo 4.0 gurus on a 68040 with no FPU - which is what Emu68 gives.

    Aminet still carries a patch for that bug, which makes the package look
    unsafe; its own history records the fix twice over, in 4.3 and again in
    4.4, and 4.4 is what the address used here serves.
    """

    def test_it_comes_from_the_address_that_serves_the_current_release(self):
        download = packages.CATALOGUE_BY_KEY["sysinfo"].download
        self.assertEqual(download.path, "util/moni/SysInfo.lha")
        self.assertNotIn("noFPU", download.path,
                         "the no-FPU patch is a .pch for 4.0, not a program")

    def test_it_lands_somewhere_it_can_be_run_from(self):
        package = packages.CATALOGUE_BY_KEY["sysinfo"]
        self.assertTrue(package.download.stage.startswith("Utilities/"))
        self.assertNotIn("Storage/Install", package.download.stage)


class MagicWbIsGone(unittest.TestCase):
    """Unsupported, unregistrable, and its Installer broke a real machine.

    Its fonts and patterns were kept for a while with the Installer withheld;
    with the software no longer supported at all it left the catalogue.
    """

    def test_it_is_not_in_the_catalogue(self):
        self.assertNotIn("magicwb", packages.CATALOGUE_BY_KEY)

    def test_nothing_suggests_it(self):
        from pistorm_imager.core.machines import Display      # noqa: PLC0415
        for machine in machines.MACHINES:
            for display in Display:
                self.assertNotIn("magicwb",
                                 packages.suggested(machine, display))


class IgameIsInstalledStandalone(unittest.TestCase):
    """Nothing from a donor. A donor's copy is whatever its author installed
    - PiMiga's is v2.1 from 2022 - and it arrives with that person's games
    list, screenshots and settings, all written against their machine. The
    release from Aminet is the whole package and starts empty, which is what
    a program that scans your own drives should do.
    """

    def setUp(self):
        self.package = packages.CATALOGUE_BY_KEY["igame"]

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


class IgameNeedsNoDonor(unittest.TestCase):
    """Everything iGame opens can be fetched, so a card built from floppies
    and Aminet alone has a working iGame. Its window is MUI, and the classes
    it uses are not part of MUI itself."""

    def test_it_declares_the_classes_it_opens(self):
        needs = set(packages.CATALOGUE_BY_KEY["igame"].requires)
        #  Not Guigfx: it and render.library are compiled for a processor
        #  with an FPU, which Emu68 does not give a PiStorm, and iGame lists
        #  them as optional.
        self.assertEqual(needs, {"mui", "mcc_nlist", "mcc_texteditor",
                                 "mcc_urltext"})

    def test_each_of_those_can_be_downloaded(self):
        for key in packages.CATALOGUE_BY_KEY["igame"].requires:
            package = packages.CATALOGUE_BY_KEY[key]
            self.assertIsNotNone(package.download,
                                 f"{key} has no download, so a card with no "
                                 f"donor cannot have iGame")

    def test_the_classes_land_where_mui_looks_for_them(self):
        for key in ("mcc_nlist", "mcc_texteditor", "mcc_urltext"):
            places = {dest for _src, dest
                      in packages.CATALOGUE_BY_KEY[key].download.items}
            self.assertIn("System/MUI/Libs/mui", places, key)

    def test_ticking_igame_brings_them_all(self):
        self.assertEqual(
            packages.expand(["igame"]),
            ["mui", "mcc_nlist", "mcc_texteditor", "mcc_urltext", "igame"])


class NothingOnTheCardNeedsAnFpu(unittest.TestCase):
    """Emu68 gives a PiStorm a 68040 with no FPU. An FPU instruction on such
    a machine is a line-F exception - guru 8000000B, which is exactly what
    iGame's own site warns about for these libraries."""

    def test_igame_does_not_ask_for_the_guigfx_stack(self):
        needs = packages.CATALOGUE_BY_KEY["igame"].requires
        self.assertNotIn("mcc_guigfx", needs)

    def test_the_guigfx_class_is_not_offered_at_all(self):
        """guigfx.library opens render.library, render.library carries 153
        floating point instructions, and no build without them exists."""
        self.assertNotIn("mcc_guigfx", packages.CATALOGUE_BY_KEY)

    def test_igame_is_told_not_to_look_for_it(self):
        written = {name: text for name, _dest, text
                   in packages.CATALOGUE_BY_KEY["igame"].download.write}
        self.assertIn("igame.prefs", written)
        self.assertIn("no_guigfx=1", written["igame.prefs"])


class FilesThisToolWritesItself(unittest.TestCase):
    """Not only for an archive laid out drawer by drawer: a settings file
    saying which of a program's optional pieces this machine can use belongs
    to any download. It was only produced on the merge path, so iGame's
    preferences were silently never written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = unittest.mock.patch.object(packages, "cache_dir",
                                           lambda: Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_plain_download_writes_them_too(self):
        package = packages.CATALOGUE_BY_KEY["igame"]
        self.assertFalse(package.download.merge, "iGame is not a merge")
        pairs = packages._written(package, _Recorder())
        names = {Path(source).name for source, _dest in pairs}
        self.assertIn("igame.prefs", names)

    def test_the_prefs_say_what_the_machine_cannot_do(self):
        pairs = packages._written(packages.CATALOGUE_BY_KEY["igame"],
                                  _Recorder())
        text = Path(pairs[0][0]).read_text()
        self.assertIn("no_guigfx=1", text)


class ABootScriptIsNotAnOperatingSystem(unittest.TestCase):
    """ClassicWB's drive has a Startup-Sequence and it is an installer: on
    the first boot it asks for a Workbench floppy and copies the copyright
    files off it, because Workbench is still sold. Reading that as a complete
    system offered a card that boots into an installer wanting a floppy
    drive."""

    def _system(self, **found):
        from pistorm_imager.core import presets
        return presets.ImageSystem(label="test", found=found)

    def test_a_boot_script_without_loadwb_still_needs_floppies(self):
        system = self._system(bootable=True, workbench=False, whdload=True)
        self.assertTrue(system.needs_floppies)
        self.assertIn("no C:LoadWB", system.describe())

    def test_a_real_system_does_not(self):
        system = self._system(bootable=True, workbench=True)
        self.assertFalse(system.needs_floppies)
        self.assertIn("complete system", system.describe())

    def test_no_boot_script_at_all_still_needs_them(self):
        self.assertTrue(self._system(bootable=False).needs_floppies)


class DrawersAreVisibleWithoutADonor(unittest.TestCase):
    """A card can be built from floppies and Aminet alone. Nothing then had a
    drawer icon to copy, so Programs - which is where every package goes -
    was created without one and did not appear on Workbench at all. iGame was
    on the card and could not be found."""

    def test_a_real_drawer_icon_comes_out_of_the_floppies(self):
        folder = Path(__file__).resolve().parent.parent / "samples" / "workbench"
        if not list(folder.glob("*.adf")):
            self.skipTest("no ADFs in samples")
        into = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, into, ignore_errors=True)
        from pistorm_imager.core import amigaos, amigainfo
        made = amigaos.drawer_icon_from_disks(folder, into)
        self.assertIsNotNone(made, "no drawer icon found on any Workbench disk")
        self.assertTrue(amigainfo.is_drawer_icon(made.read_bytes()),
                        "what was taken is not a drawer icon")

    def test_nothing_is_taken_from_a_folder_with_no_disks(self):
        from pistorm_imager.core import amigaos
        into = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, into, ignore_errors=True)
        self.assertIsNone(amigaos.drawer_icon_from_disks(into, into))


class ACardThatCannotBootIsRefused(unittest.TestCase):
    """ClassicWB's drive boots and carries no Workbench: no C:LoadWB, no
    C:IPrefs, no Version, because those are Commodore's. Built without the
    disks it stops at a Shell saying "C:Version: Unknown command" - and
    taking its own installer away, which is what finishing the install does,
    makes that worse rather than better."""

    def _config(self, hdf, **kw):
        from pistorm_imager.core import builder
        return builder.BuildConfig(
            target="/tmp/card.img",
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0, content_hdf=str(hdf))], **kw)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_drive_with_no_workbench_and_no_disks_is_refused(self):
        from pistorm_imager.core import builder, presets
        with unittest.mock.patch.object(
                presets, "inspect_image_system",
                lambda *a, **k: presets.ImageSystem(found={"bootable": True})):
            with self.assertRaises(RuntimeError) as caught:
                builder._check_the_system_can_boot(
                    self._config("/somewhere/System.hdf"), None)
        self.assertIn("cannot boot", str(caught.exception))

    def test_with_the_disks_it_is_allowed(self):
        from pistorm_imager.core import builder, presets
        with unittest.mock.patch.object(
                presets, "inspect_image_system",
                lambda *a, **k: presets.ImageSystem(found={"bootable": True})):
            builder._check_the_system_can_boot(
                self._config("/somewhere/System.hdf", install_amigaos=True,
                             adf_folder="/disks"), None)

    def test_a_drive_that_brings_its_own_is_fine(self):
        from pistorm_imager.core import builder, presets
        with unittest.mock.patch.object(
                presets, "inspect_image_system",
                lambda *a, **k: presets.ImageSystem(
                    found={"bootable": True, "workbench": True})):
            builder._check_the_system_can_boot(
                self._config("/somewhere/System.hdf"), None)


class ChoicesThatBuildAndMislead(unittest.TestCase):
    """Distinct from what is refused: combinations that produce a working
    card doing something other than the settings suggest. They are said
    before anything is written, and the build goes ahead."""

    def _config(self, **kw):
        from pistorm_imager.core import builder
        parts = kw.pop("partitions", None) or [
            builder.AmigaPartitionSpec("DH0", 1024 ** 3, "PFS3", True, 0)]
        return builder.BuildConfig(target="/tmp/card.img",
                                   amiga_partitions=parts, **kw)

    def _games_drive(self):
        from pistorm_imager.core import builder
        return [builder.AmigaPartitionSpec("DH0", 1024 ** 3, "PFS3", True, 0),
                builder.AmigaPartitionSpec("DH1", None, "PFS3", False, -128,
                                           volume_name="Games",
                                           content_folder="/x/Games")]

    def test_games_with_nothing_to_launch_them(self):
        said = self._config(partitions=self._games_drive()).concerns()
        self.assertTrue([s for s in said if "WHDLoad is not installed" in s])

    def test_and_not_when_whdload_is_there(self):
        said = self._config(partitions=self._games_drive(),
                            package_keys=["whdload"]).concerns()
        self.assertFalse([s for s in said if "WHDLoad is not installed" in s])

    def test_igame_with_no_games(self):
        said = self._config(package_keys=["igame", "whdload"]).concerns()
        self.assertTrue([s for s in said if "empty list" in s])

    def test_workbench_on_a_screen_the_card_has_not_got(self):
        said = self._config(workbench_on_rtg=True, rtg_display=False).concerns()
        self.assertTrue([s for s in said if "no RTG display" in s])

    def test_an_rtg_card_with_no_rtg_driver(self):
        said = self._config(rtg_display=True).concerns()
        self.assertTrue([s for s in said if "no RTG screen to open on" in s])

    def test_software_nobody_can_fetch_on_your_behalf(self):
        #  Roadshow's publisher serves the archive only to a browser, so the
        #  card is built without it unless a copy is already cached.
        said = self._config(package_keys=["roadshow"]).concerns()
        self.assertTrue([s for s in said if "roadshow" in s], said)

    def test_a_card_with_nothing_on_its_drives(self):
        said = self._config().concerns()
        self.assertTrue([s for s in said if "Nothing is being put" in s])


class TheCardSaysWhatWasPutOnIt(unittest.TestCase):
    """AmigaOS has no uninstaller, so the card carries its own record.

    Commodore's Installer never had a removal facility, and nothing this
    imager installs goes through Installer at all - the files are copied into
    place directly - so there is no install log for any of the Aminet
    uninstallers to work from. Taking a package off a finished card meant
    reading an hour-old build log, if it was still on the screen. The card now
    says so itself, in S:PiStorm-Installed.
    """

    def setUp(self):
        from pistorm_imager.core import builder                # noqa: PLC0415
        self.builder = builder
        self.folder = Path(tempfile.mkdtemp(prefix="pistorm-manifest-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def _package_with_its_own_drawer(self) -> str:
        drawer = self.folder / "PowerWindows"
        (drawer / "Docs").mkdir(parents=True)
        (drawer / "PowerWindows").write_bytes(b"prog")
        (drawer / "Docs" / "PowerWindows.guide").write_bytes(b"doc")
        return str(drawer)

    def _package_merged_into_the_system(self) -> str:
        commands = self.folder / "whdload-c"
        commands.mkdir()
        (commands / "WHDLoad").write_bytes(b"prog")
        (commands / "WHDLoadCD32").write_bytes(b"prog")
        return str(commands)

    def test_a_packages_own_drawer_is_named_as_a_drawer(self):
        pairs = [(self._package_with_its_own_drawer(),
                  "Utilities/PowerWindows")]
        landed = {pairs[0]: (["Utilities/PowerWindows/PowerWindows",
                              "Utilities/PowerWindows/Docs/PowerWindows.guide"],
                             True)}
        entries = self.builder._manifest_entries(pairs,
                                                 {pairs[0]: "powerwindows"},
                                                 landed)
        self.assertEqual([label for label, _ in entries], ["PowerWindows"],
                         "the package's own name is what a reader can act on, "
                         "not the key this code happens to file it under")
        listed = entries[0][1]
        self.assertEqual(len(listed), 1,
                         "a package's own drawer is one line, not a file list")
        self.assertTrue(listed[0].startswith("Utilities/PowerWindows"),
                        listed[0])
        self.assertIn("2 files", listed[0],
                      "the count is what says whether it is worth deleting")

    def test_files_merged_into_a_system_drawer_are_listed_one_by_one(self):
        #  The dangerous case. WHDLoad puts its commands into C, which
        #  AmigaDOS owns: naming the drawer would read as "Delete SYS:C ALL"
        #  and take the operating system with it.
        pairs = [(self._package_merged_into_the_system(), "C")]
        entries = self.builder._manifest_entries(pairs, {pairs[0]: "whdload"})
        listed = entries[0][1]
        self.assertIn("C/WHDLoad", listed)
        self.assertIn("C/WHDLoadCD32", listed)
        self.assertNotIn("C", listed, "the whole C: drawer must never be named")
        for line in listed:
            self.assertNotIn("whole drawer", line, line)

    def test_the_startup_lines_are_recorded_too(self):
        config = self.builder.BuildConfig(target="/tmp/x",
                                          package_keys=["fblit"])
        pairs = [(self._package_with_its_own_drawer(),
                  "Utilities/PowerWindows")]
        text = self.builder._manifest_text(
            config, pairs, {pairs[0]: "powerwindows"},
            {pairs[0]: (["Utilities/PowerWindows/PowerWindows"], True)})
        self.assertIn("User-Startup", text,
                      "a line left behind in User-Startup runs a program that "
                      "is no longer there")
        self.assertIn("C:FBlit", text)

    def test_nothing_installed_means_no_file_at_all(self):
        config = self.builder.BuildConfig(target="/tmp/x")
        self.assertEqual(self.builder._manifest_text(config, [], {}), "")

    def test_it_really_lands_on_the_drive(self):
        #  Not the pure function alone: the last time a helper was tested
        #  without its caller, the caller was missing an import and every
        #  build died on it while the tests stayed green.
        from pistorm_imager.core import amigaos, pfs3, rdb      # noqa: PLC0415
        image = self.folder / "drive.hdf"
        size = 8 * 1024 * 1024
        with open(image, "wb") as handle:
            handle.truncate(size)
        pairs = [(self._package_merged_into_the_system(), "C")]
        config = self.builder.BuildConfig(target="/tmp/x",
                                          package_keys=["whdload"])
        with open(image, "r+b") as handle:
            volume = amigaos.make_volume(handle, 0, size // 512, "Test",
                                         rdb.DOSTYPE_PFS3)
            self.builder._write_manifest(volume, config, pairs,
                                         {pairs[0]: "whdload"}, Progress())
            volume.close()
        with open(image, "rb") as handle:
            back = pfs3.Pfs3Volume(handle, 0)
            entry = back.find(self.builder.MANIFEST_PATH)
            self.assertIsNotNone(entry, "no record was written to the card")
            text = back.read_file(entry).decode("latin-1")
        self.assertIn("C/WHDLoad", text)
        self.assertIn("Delete SYS:", text, "it has to say how to use it")

    def test_an_imported_drives_old_record_is_not_left_standing(self):
        #  A card built from a drive that this imager built before brings the
        #  earlier build's manifest with it. Read afterwards it would describe
        #  software that is not there and miss software that is.
        fixer = compat.Compatibility(Progress())
        fixer.displace([self.builder.MANIFEST_PATH])
        fixer.offer(self.builder.MANIFEST_PATH, b"; an older card")
        self.assertTrue(fixer.skip(self.builder.MANIFEST_PATH),
                        "the old record must be held back so ours can land")

    def test_a_real_build_writes_it(self):
        #  The whole path, not the writer alone: a helper can be correct and
        #  never called, which is how a missing import once broke every build
        #  with the tests still green.
        from pistorm_imager.core import mbr, pfs3               # noqa: PLC0415
        drive = self.folder / "drive"
        (drive / "S").mkdir(parents=True)
        (drive / "S" / "Startup-Sequence").write_bytes(b"Echo hello\n")
        #  The drive brings a library of its own, and an overlay offers the
        #  same one. It is not written, so it is not this build's to claim.
        (drive / "Libs").mkdir()
        (drive / "Libs" / "guigfx.library").write_bytes(b"the drive's own")
        overlay = self.folder / "PowerWindows"
        overlay.mkdir()
        (overlay / "PowerWindows").write_bytes(b"prog")
        offered = self.folder / "libs-overlay"
        offered.mkdir()
        (offered / "guigfx.library").write_bytes(b"ours")
        (offered / "identify.library").write_bytes(b"ours")

        image = self.folder / "out.hdf"
        size = 200 * 1024 * 1024
        with open(image, "wb") as handle:
            handle.truncate(size)
        config = self.builder.BuildConfig(
            target=str(image), install_amigaos=False,
            amiga_partitions=[self.builder.AmigaPartitionSpec(
                name="DH0", size=180 * 1024 * 1024, bootable=True,
                volume_name="System", content_folder=str(drive),
                overlays=[(str(overlay), "Utilities/PowerWindows"),
                          (str(offered), "Libs")])])
        progress = Progress()
        with open(image, "r+b") as handle:
            table = self.builder._build_rdb(config, size // 512, progress)
            table.write(handle, 0)
            whole = mbr.MbrPartition(0, 0, mbr.TYPE_AMIGA, 0, size // 512)
            self.builder._install_content(config, handle, whole, table,
                                          progress)
        with open(image, "rb") as handle:
            offset = table.partitions[0].byte_offset(table.geometry, 0)
            volume = pfs3.Pfs3Volume(handle, offset)
            entry = volume.find(self.builder.MANIFEST_PATH)
            self.assertIsNotNone(entry,
                                 "a finished drive carries no record of what "
                                 "this build put on it")
            text = volume.read_file(entry).decode("latin-1")
        self.assertIn("Utilities/PowerWindows", text)
        self.assertIn("whole drawer, 1 file", text)
        self.assertIn("Libs/identify.library", text,
                      "the library this build really wrote is missing")
        self.assertNotIn("guigfx", text,
                         "the drive's own library was skipped as already "
                         "present, so claiming it is an instruction to delete "
                         "somebody else's file")

    def test_a_name_that_is_not_utf8_does_not_end_the_build(self):
        #  MUI ships Locale/Catalogs/francais with a latin-1 cedilla, which
        #  arrives from the host as a lone surrogate. A plain latin-1 encode
        #  raises on it, and this file is written at the very last step of a
        #  build that takes an hour: it took the whole card with it, unclosed
        #  and unformatted, after every file had already been copied.
        from pistorm_imager.core import amigaos, pfs3, rdb      # noqa: PLC0415
        merged = self.folder / "catalogs"
        (merged / "fran\udce7ais").mkdir(parents=True)
        (merged / "fran\udce7ais" / "mui.catalog").write_bytes(b"cat")
        pairs = [(str(merged), "Locale/Catalogs")]
        config = self.builder.BuildConfig(target="/tmp/x")

        text = self.builder._manifest_text(config, pairs, {})
        self.assertIn("Locale/Catalogs/fran\udce7ais/mui.catalog", text)
        #  Written back as the byte the Amiga had in the first place.
        self.assertIn(b"fran\xe7ais", self.builder._amiga_bytes(text))

        image = self.folder / "cat.hdf"
        size = 8 * 1024 * 1024
        with open(image, "wb") as handle:
            handle.truncate(size)
        with open(image, "r+b") as handle:
            volume = amigaos.make_volume(handle, 0, size // 512, "Test",
                                         rdb.DOSTYPE_PFS3)
            self.builder._write_manifest(volume, config, pairs, {}, Progress())
            volume.close()
        with open(image, "rb") as handle:
            back = pfs3.Pfs3Volume(handle, 0)
            self.assertIsNotNone(back.find(self.builder.MANIFEST_PATH))

    def test_nothing_it_can_do_ends_a_build(self):
        #  Whatever else goes wrong in here, an hour of copying is not thrown
        #  away for a file that is only a convenience.
        class Broken:
            def makedirs(self, _where):
                raise RuntimeError("the drive said no")

        progress = Progress()
        said = []
        progress.log = said.append
        config = self.builder.BuildConfig(target="/tmp/x")
        pairs = [(self._package_with_its_own_drawer(),
                  "Utilities/PowerWindows")]
        self.builder._write_manifest(Broken(), config, pairs,
                                     {pairs[0]: "powerwindows"}, progress)
        self.assertTrue(any("WARNING" in line and "card is unaffected" in line
                            for line in said),
                        f"the failure has to be reported, not swallowed: {said}")

    def test_a_drawer_the_system_owns_is_never_named_however_it_arrived(self):
        #  The near-miss. Every package that merges into a system drawer does
        #  so under exactly that drawer's own name - the build log shows
        #  "Libs/ -> Libs", "C/ -> C" and "S/ -> S" - so deciding by whether
        #  the source drawer's name matched the destination would have written
        #  "C ; whole drawer" into a file whose header says to delete what it
        #  lists. That is "Delete SYS:C ALL", and the end of AmigaDOS.
        for name in ("C", "Libs", "S", "L", "Devs", "WBStartup", "Prefs"):
            with self.subTest(drawer=name):
                source = self.folder / "same" / name
                source.mkdir(parents=True, exist_ok=True)
                (source / "thing").write_bytes(b"x")
                pairs = [(str(source), name)]
                listed = self.builder._manifest_entries(pairs, {})[0][1]
                self.assertEqual(listed, [f"{name}/thing"],
                                 f"{name} must be listed file by file")
                for line in listed:
                    self.assertNotIn("whole drawer", line, line)

    def test_a_drawer_the_package_brought_is_named_even_if_renamed(self):
        #  WookieChat unpacks as "WookieChat2.11_OS3_Installer" and lands in
        #  Internet/WookieChat. Matching on the source name listed all 145 of
        #  its files; the destination is the package's own drawer, so one line
        #  says it.
        source = self.folder / "WookieChat2.11_OS3_Installer"
        (source / "Docs").mkdir(parents=True)
        (source / "WookieChat").write_bytes(b"x")
        (source / "Docs" / "readme").write_bytes(b"x")
        pairs = [(str(source), "Internet/WookieChat")]
        landed = {pairs[0]: (["Internet/WookieChat/WookieChat",
                              "Internet/WookieChat/Docs/readme"], True)}
        listed = self.builder._manifest_entries(pairs, {}, landed)[0][1]
        self.assertEqual(len(listed), 1, listed)
        self.assertIn("Internet/WookieChat  ; whole drawer, 2 files", listed[0])

    def test_the_drawers_this_build_shares_out_are_not_named_either(self):
        #  Several packages land side by side in Internet, Audio and Programs.
        #  Naming one of those as a package's own drawer would take the others
        #  with it.
        for name in ("Internet", "Audio", "Programs", "Storage/Install"):
            with self.subTest(drawer=name):
                source = self.folder / "shared" / name.replace("/", "-")
                source.mkdir(parents=True, exist_ok=True)
                (source / "thing").write_bytes(b"x")
                pairs = [(str(source), name)]
                listed = self.builder._manifest_entries(pairs, {})[0][1]
                self.assertEqual(listed, [f"{name}/thing"])

    def test_only_what_was_really_written_is_claimed(self):
        #  Read off a finished card. NewInstaller's overlay offers five
        #  libraries the drive already had - the log says "skipped
        #  guigfx.library: already exists" - and the record claimed all five
        #  as its own. Following it would have deleted ClassicWB's copies
        #  under NewInstaller's name.
        source = self.folder / "newinstaller"
        source.mkdir()
        for name in ("NewInstaller", "guigfx.library", "render.library"):
            (source / name).write_bytes(b"x")
        pairs = [(str(source), "Libs")]
        #  Only the first was actually written; the drive already had the
        #  other two.
        landed = {pairs[0]: (["Libs/NewInstaller"], False)}
        listed = self.builder._manifest_entries(pairs, {}, landed)[0][1]
        self.assertEqual(listed, ["Libs/NewInstaller"])
        self.assertNotIn("Libs/guigfx.library", listed,
                         "a file this build did not write must never be "
                         "offered up for deletion")

    def test_a_drawer_the_drive_already_had_is_not_claimed_whole(self):
        #  ClassicWB brings its own System/MUI. Our MUI merges 56 files into
        #  it and skips 339, so naming the drawer would hand over the drive's
        #  MUI as though this build had put it there.
        source = self.folder / "MUI"
        source.mkdir()
        (source / "muimaster.library").write_bytes(b"x")
        pairs = [(str(source), "System/MUI")]
        landed = {pairs[0]: (["System/MUI/muimaster.library"], False)}
        listed = self.builder._manifest_entries(pairs, {}, landed)[0][1]
        self.assertEqual(listed, ["System/MUI/muimaster.library"])
        for line in listed:
            self.assertNotIn("whole drawer", line, line)

    def test_the_copy_reports_only_the_files_it_wrote(self):
        #  The plumbing this rests on: install_tree's report must exclude a
        #  file the volume already had.
        from pistorm_imager.core import amigaos, rdb                # noqa: PLC0415
        source = self.folder / "overlay"
        source.mkdir()
        (source / "new").write_bytes(b"new")
        (source / "old").write_bytes(b"ours")
        image = self.folder / "landings.hdf"
        size = 8 * 1024 * 1024
        with open(image, "wb") as handle:
            handle.truncate(size)
        with open(image, "r+b") as handle:
            volume = amigaos.make_volume(handle, 0, size // 512, "Test",
                                         rdb.DOSTYPE_PFS3)
            drawer = volume.makedirs("Libs")
            volume.write_file(drawer, "old", b"the drive's own",
                              check_existing=False)
            written = []
            amigaos.install_tree(volume, source, "Libs", Progress(),
                                 merge=True, written=written)
            volume.close()
        self.assertEqual(written, ["Libs/new"],
                         "the file the drive already had was not written by "
                         f"this build and must not be reported: {written}")


class TheCardSizeSurvivesBeingShown(unittest.TestCase):
    """An image built to a card's size has to actually fit that card.

    A 64 GB card holding 63,864,569,856 bytes was written into the size box
    as "59.48 GiB" - human_size rounds to two decimals of a GiB, which is
    steps of 10.7 MB - and building an image file reads that box back. It
    came back as 63,866,163,691: an image 1.6 MB too big for the card it was
    measured from. Found by writing it.
    """

    #  Real capacities, all multiples of the sector size.
    CARDS = (63864569856, 31914983424, 127865454592, 15931539456,
             255013289984)

    def test_a_cards_size_reads_back_as_the_same_number(self):
        from pistorm_imager.core.util import exact_size_text     # noqa: PLC0415
        from pistorm_imager.ui.window import parse_size          # noqa: PLC0415
        for size in self.CARDS:
            with self.subTest(card=size):
                self.assertEqual(parse_size(exact_size_text(size)), size)

    def test_the_rounded_text_is_the_one_that_does_not(self):
        #  The guard is only worth anything if it fails for the old text.
        #  Rounding to two decimals of a GiB misses in both directions: a
        #  card that reads back smaller quietly wastes up to 10 MB, and one
        #  that reads back larger will not take the image at all. Every one
        #  of these five real capacities is wrong one way or the other.
        from pistorm_imager.core.util import human_size          # noqa: PLC0415
        from pistorm_imager.ui.window import parse_size          # noqa: PLC0415
        wrong = {size: parse_size(human_size(size)) for size in self.CARDS
                 if parse_size(human_size(size)) != size}
        self.assertEqual(len(wrong), len(self.CARDS),
                         "human_size is lossy for every real card capacity")
        too_big = [size for size, back in wrong.items() if back > size]
        self.assertTrue(too_big, "the case that costs a write: an image "
                                 "larger than the card it was measured from")

    def test_an_image_measured_from_a_card_is_never_bigger_than_it(self):
        from pistorm_imager.core.util import exact_size_text     # noqa: PLC0415
        from pistorm_imager.ui.window import parse_size          # noqa: PLC0415
        for size in self.CARDS:
            with self.subTest(card=size):
                self.assertLessEqual(parse_size(exact_size_text(size)), size,
                                     "an image larger than the card it was "
                                     "measured from cannot be written to it")


class AnythingInWBStartupHasToActuallyStart(unittest.TestCase):
    """Workbench runs the icons in WBStartup, not the files.

    Two opposite failures, both found by reading a finished card:

    A program with **no icon** is never started at all. DefIcons, FullPalette
    and MagicMenu were each copied into WBStartup without theirs, so every
    card this tool has built carried three programs that could not run - the
    missing colour icons being the visible half of it.

    A program **with** an icon and no DONOTWAIT stops the boot, because
    Workbench waits for it to exit and a commodity never does. FullPalette's
    own icon, as shipped, has no DONOTWAIT: supplying the icons without also
    checking for it would have turned three inert programs into a card that
    does not finish booting.
    """

    def _wbstartup_items(self):
        from pistorm_imager.core import packages                # noqa: PLC0415
        for package in packages.CATALOGUE:
            download = getattr(package, "download", None)
            if download is None:
                continue
            landings = [(inside, dest) for inside, dest in download.items]
            landings += [(inside, dest) for inside, dest, _new
                         in download.rename]
            for inside, dest in landings:
                if str(dest).strip("/").lower() == "wbstartup":
                    yield package, inside, dest

    def test_every_program_put_in_wbstartup_brings_its_icon(self):
        from pistorm_imager.core import packages                # noqa: PLC0415
        for package in packages.CATALOGUE:
            download = getattr(package, "download", None)
            if download is None:
                continue
            #  The name each item lands under, which for a rename is the new
            #  one - that is what Workbench matches an icon against.
            landed = [Path(inside).name for inside, dest in download.items
                      if str(dest).strip("/").lower() == "wbstartup"]
            landed += [new for _inside, dest, new in download.rename
                       if str(dest).strip("/").lower() == "wbstartup"]
            programs = [n for n in landed if not n.lower().endswith(".info")]
            icons = {n.lower() for n in landed if n.lower().endswith(".info")}
            for program in programs:
                with self.subTest(package=package.key, program=program):
                    self.assertIn(f"{program.lower()}.info", icons,
                                  f"{package.label} puts {program} in "
                                  f"WBStartup with no icon, so Workbench "
                                  f"will never start it")

    def test_an_icon_without_donotwait_is_given_one(self):
        from pistorm_imager.core import amigainfo, compat        # noqa: PLC0415
        plain = amigainfo.write_tooltypes(_an_icon(), ["CX_PRIORITY=0"])
        self.assertNotIn("DONOTWAIT", amigainfo.read_tooltypes(plain))
        fixer = compat.Compatibility(Progress())
        fixed = fixer.offer("WBStartup/FullPalette.info", plain)
        self.assertIn("DONOTWAIT", amigainfo.read_tooltypes(fixed))
        self.assertIn("CX_PRIORITY=0", amigainfo.read_tooltypes(fixed),
                      "the icon's own tool types must survive")

    def test_an_icon_that_has_it_is_left_exactly_alone(self):
        from pistorm_imager.core import amigainfo, compat        # noqa: PLC0415
        already = amigainfo.write_tooltypes(_an_icon(), ["DONOTWAIT"])
        fixer = compat.Compatibility(Progress())
        self.assertEqual(fixer.offer("WBStartup/FreeWheel.info", already),
                         already)

    def test_only_wbstartup_is_touched(self):
        #  ClickToFront's icon has no DONOTWAIT and does not need one: it
        #  lives in Utilities, where nothing starts it.
        from pistorm_imager.core import amigainfo, compat        # noqa: PLC0415
        plain = amigainfo.write_tooltypes(_an_icon(), [])
        fixer = compat.Compatibility(Progress())
        self.assertEqual(
            fixer.offer("Utilities/ClickToFront/ClickToFront.info", plain),
            plain)

    def test_something_that_is_not_an_icon_is_not_mangled(self):
        #  MagicMenu ships an "icon" that is a PNG file with an .info name.
        #  Workbench 3.1 cannot read it, and neither can this - but it must
        #  come through untouched rather than half-rewritten.
        from pistorm_imager.core import compat                   # noqa: PLC0415
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 200
        fixer = compat.Compatibility(Progress())
        self.assertEqual(fixer.offer("WBStartup/MagicMenu.info", png), png)

    def test_each_icon_named_is_one_workbench_can_read(self):
        #  MagicMenu ships two icon sets, and the obvious pick was wrong:
        #  Icons/DualPNG/MagicMenu.info is a PNG file with an .info name, an
        #  OS4 icon that Workbench 3.1 does not read. Supplying it would have
        #  left MagicMenu exactly as unstarted as no icon at all, and the
        #  catalogue cannot tell the difference by name.
        from pistorm_imager.core import amigainfo, packages      # noqa: PLC0415
        cache = packages.cache_dir()
        checked = 0
        for package, inside, _dest in self._wbstartup_items():
            if not inside.lower().endswith(".info"):
                continue
            archive = getattr(package.download, "url", "") or ""
            stem = Path(archive).name
            for suffix in (".lha", ".zip", ".run"):
                stem = stem.removesuffix(suffix)
            path = cache / f"{stem}.unpacked" / inside
            if not path.exists():
                continue                     # not fetched on this machine
            checked += 1
            with self.subTest(package=package.key, icon=inside):
                try:
                    amigainfo.read_tooltypes(path.read_bytes())
                except amigainfo.InfoError as error:
                    self.fail(f"{package.label} names {inside}, which is not "
                              f"an icon Workbench 3.1 can read ({error})")
        if not checked:
            self.skipTest("no package archives are unpacked on this machine")

    def test_the_installer_that_asks_is_not_started_at_boot(self):
        #  ClickToFront's archive holds Bryce Nesbitt's 1987 original, whose
        #  only mode is a requester offering Install or Cancel. In WBStartup
        #  it asked on every single boot.
        from pistorm_imager.core import packages                 # noqa: PLC0415
        where = {dest for _inside, dest
                 in packages.CATALOGUE_BY_KEY["clicktofront"].download.items}
        self.assertNotIn("WBStartup", where)
        for dest in where:
            self.assertTrue(dest.startswith("Utilities/"), dest)


def _an_icon() -> bytes:
    """The smallest DiskObject this project's icon reader accepts."""
    import struct                                                # noqa: PLC0415
    from pistorm_imager.core import amigainfo                    # noqa: PLC0415
    data = bytearray(78)
    struct.pack_into(">H", data, 0, 0xE310)      # ic_Magic
    struct.pack_into(">H", data, 2, 1)           # ic_Version
    data[48] = 3                                 # do_Type: a tool
    struct.pack_into(">I", data, 54, 1)          # do_ToolTypes, non-zero
    data += struct.pack(">I", 4)                 # an empty tool type array
    amigainfo.read_tooltypes(bytes(data))        # it has to parse
    return bytes(data)



class NewDrawersLookLikeTheDesktopTheyJoin(unittest.TestCase):
    """A drive being imported brings a desktop that somebody designed.

    ClassicWB's drawers are MagicWB-styled. The drawers this tool adds beside
    them - Internet/NetSurf, Utilities/SysInfo - were given a stock Workbench
    3.1 drawer icon, because the only icons on offer came off the floppies, so
    the software the user chose was the part of the desktop that looked
    foreign. Reported after a card was written.
    """

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="pistorm-drawericon-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    @staticmethod
    def _icon(kind: int) -> bytes:
        """A DiskObject the icon reader accepts, drawer or tool."""
        import struct                                            # noqa: PLC0415
        from pistorm_imager.core import amigainfo                # noqa: PLC0415
        data = bytearray(amigainfo.DISKOBJECT_SIZE)
        struct.pack_into(">H", data, 0, amigainfo.MAGIC)
        struct.pack_into(">H", data, 2, 1)
        data[amigainfo.TYPE_OFFSET] = kind
        if kind == amigainfo.WBDRAWER:
            #  A drawer icon has to carry DrawerData, or it cannot open a
            #  window and is no use to a drawer.
            struct.pack_into(">I", data, amigainfo.DRAWER_DATA, 1)
        return bytes(data) + struct.pack(">I", 4)

    def _reader(self, entries):
        icon = self._icon
        class Entry:
            def __init__(self, name): self.name = name
        class Reader:
            def listdir(self, *_a): return [Entry(n) for n in entries]
            def read_file(self, entry): return entries[entry.name]
        return Reader()

    def test_the_drives_own_drawer_icons_are_taken(self):
        from pistorm_imager.core import amigaos                  # noqa: PLC0415
        entries = {
            "Utilities": b"", "Utilities.info": self._icon(2),
            "Internet": b"", "Internet.info": self._icon(2),
            #  A tool icon is not a drawer icon: opening a drawer with one
            #  gave "unable to open script" the last time it was tried.
            "Shell": b"", "Shell.info": self._icon(3),
            #  An icon whose drawer is not there belongs to something else.
            "Gone.info": self._icon(2),
        }
        into = self.folder / "borrowed"
        found = amigaos.drawer_icons_from_volume(self._reader(entries), into)
        self.assertEqual(found, 2)
        self.assertEqual(sorted(p.name for p in into.iterdir()),
                         ["Internet.info", "Utilities.info"])

    def test_the_drive_is_preferred_over_the_floppies(self):
        #  Both offer a "utilities" drawer icon. The one already on the
        #  desktop has to win, or the new drawers stay the odd ones out.
        from pistorm_imager.core import amigaos                  # noqa: PLC0415
        drive = self.folder / "drive"
        floppy = self.folder / "floppy"
        drive.mkdir(); floppy.mkdir()
        mine, theirs = self._icon(2), self._icon(2) + b"\0" * 8
        (drive / "Utilities.info").write_bytes(mine)
        (floppy / "Utilities.info").write_bytes(theirs)
        found = amigaos._drawer_icon_sources([drive, floppy])
        self.assertEqual(found["utilities"], mine,
                         "the floppies' icon overwrote the drive's own")

    def test_the_generic_icon_comes_from_the_drive_too(self):
        #  The whole point, and it did not work: the drive's icons were
        #  harvested and merged, but the stand-in for a drawer whose name
        #  matched nothing was chosen by name across all sources - and the
        #  Workbench floppies contribute one called literally "drawer",
        #  which beat every name after it. So a MagicWB desktop still got
        #  plain 3.1 drawers for exactly the software the user chose. Found
        #  by building a card and comparing the icons byte for byte.
        from pistorm_imager.core import amigaos                  # noqa: PLC0415
        drive = self.folder / "drive"
        floppy = self.folder / "floppy"
        drive.mkdir(); floppy.mkdir()
        mine = self._icon(2)
        (drive / "Utilities.info").write_bytes(mine)
        (floppy / "drawer.info").write_bytes(self._icon(2) + b"\0" * 8)
        self.assertEqual(amigaos._generic_drawer_icon([drive, floppy]), mine,
                         "the floppies' drawer icon won again")

    def test_the_floppies_are_still_there_to_fall_back_on(self):
        from pistorm_imager.core import amigaos                  # noqa: PLC0415
        floppy = self.folder / "only-floppy"
        floppy.mkdir()
        theirs = self._icon(2) + b"\0" * 8
        (floppy / "drawer.info").write_bytes(theirs)
        self.assertEqual(amigaos._generic_drawer_icon([floppy]), theirs)
        self.assertIsNone(amigaos._generic_drawer_icon([]))

    def test_a_card_with_no_drive_still_gets_icons(self):
        #  Built from floppies alone there is no drive to copy from, and the
        #  drawers must still be visible.
        from pistorm_imager.core import builder                  # noqa: PLC0415
        spec = builder.AmigaPartitionSpec(name="DH0", bootable=True)
        self.assertIsNone(builder._drawer_icons_from_the_drive(spec, Progress()))


class ADistributionsOwnOlderCopyIsReplaced(unittest.TestCase):
    """A prepared drive can carry the same program under its own name.

    ClassicWB FULL keeps SysInfo 3.24 from 1993 in Tools/SysInfo while the
    package installs 4.4 into Utilities/SysInfo, and Directory Opus 4.16 in
    Programs/DirOpus4 beside the package's 4.18.22 in Programs/DirectoryOpus.
    Both copies landed and only one of each was ever opened, because the
    displacement that replaces an older copy matches on path and these sit
    somewhere this build would never write.
    """

    def _fixer(self):
        from pistorm_imager.core import compat                   # noqa: PLC0415
        return compat.Compatibility(Progress())

    def test_the_whole_drawer_goes(self):
        fixer = self._fixer()
        fixer.supersede(["Tools/SysInfo"])
        for path in ("Tools/SysInfo", "Tools/SysInfo/SysInfo",
                     "Tools/SysInfo/Docs/SysInfo.guide"):
            self.assertTrue(fixer.skip(path), path)

    def test_it_does_not_reach_past_the_drawer(self):
        #  The dangerous direction: a prefix match that is not a path
        #  boundary would take Tools/SysInfoExtra with it.
        fixer = self._fixer()
        fixer.supersede(["Tools/SysInfo"])
        for path in ("Tools/SysInfoExtra/SysInfo", "Tools/Other/SysInfo",
                     "Utilities/SysInfo/SysInfo", "Tools/SysInfoDocs"):
            self.assertFalse(fixer.skip(path), path)

    def test_the_newer_copy_this_build_installs_is_untouched(self):
        #  The point of removing theirs is to leave ours.
        fixer = self._fixer()
        fixer.supersede(["Tools/SysInfo"])
        self.assertFalse(fixer.skip("Utilities/SysInfo/SysInfo"))

    def test_it_is_reported_once_not_once_per_file(self):
        fixer = self._fixer()
        fixer.supersede(["Programs/DirOpus4"])
        for path in ("Programs/DirOpus4/DirectoryOpus",
                     "Programs/DirOpus4/Docs/a", "Programs/DirOpus4/libs/b"):
            fixer.skip(path)
        replaced = [f for f in fixer.fixes if "DirOpus4" in str(f)]
        self.assertEqual(len(replaced), 1, replaced)
        self.assertEqual(fixer.superseded_drawers(), ["Programs/DirOpus4"],
                         "the log should name the drawer as the drive does")

    def test_the_package_still_lands_in_the_drawer_it_emptied(self):
        #  Read off a written card: "Programs/iGame left out whole" followed
        #  by "overlay: iGame-v2.6.1/ -> Programs/iGame (0 files)". Removing
        #  the drive's older copy so the chosen one can take its place then
        #  refused the chosen one too, because it lands in the very drawer
        #  being emptied - so the card arrived with a launcher drawer holding
        #  an icon and nothing else.
        from pistorm_imager.core import amigaos, compat, pfs3, rdb  # noqa: PLC0415
        folder = Path(tempfile.mkdtemp(prefix="pistorm-supersede3-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        drive = folder / "drive"
        (drive / "Programs/iGame").mkdir(parents=True)
        (drive / "Programs/iGame/iGame").write_bytes(b"the drive's old one")
        ours = folder / "ours"
        ours.mkdir()
        (ours / "iGame").write_bytes(b"the one that was chosen")

        image = folder / "out.hdf"
        size = 16 * 1024 * 1024
        with open(image, "wb") as handle:
            handle.truncate(size)
        with open(image, "r+b") as handle:
            volume = amigaos.make_volume(handle, 0, size // 512, "Test",
                                         rdb.DOSTYPE_PFS3)
            fixer = compat.Compatibility(Progress())
            fixer.supersede(["Programs/iGame"])
            amigaos.install_tree(volume, drive, "", Progress(), compat=fixer)
            #  What the build does between filling the drive and laying the
            #  packages on top.
            fixer.stop_displacing()
            amigaos.install_tree(volume, ours, "Programs/iGame", Progress(),
                                 compat=fixer, merge=True)
            volume.close()

        with open(image, "rb") as handle:
            back = pfs3.Pfs3Volume(handle, 0)
            entry = back.find("Programs/iGame/iGame")
            self.assertIsNotNone(entry, "the chosen copy never landed")
            self.assertEqual(back.read_file(entry), b"the one that was chosen",
                             "the drive's older copy is what survived")

    def test_a_drawer_overlay_still_merges(self):
        #  Superseding is a separate set from displacement for a reason: MUI's
        #  overlay claims the name System/MUI so a *file* of that name cannot
        #  block it, and must still merge into ClassicWB's own MUI rather
        #  than replace it. Making displacement prefix-match would have
        #  emptied that drawer.
        fixer = self._fixer()
        fixer.displace(["System/MUI"])
        self.assertTrue(fixer.skip("System/MUI"))
        self.assertFalse(fixer.skip("System/MUI/muimaster.library"),
                         "the drive's own MUI files must still be copied")

    def test_nothing_is_removed_unless_the_user_said_so(self):
        #  Nothing about which programs exist is written into the source any
        #  more: the removals are exactly the answers that came back from the
        #  lists, and a build with no answers removes nothing.
        from pistorm_imager.core import builder                  # noqa: PLC0415
        config = builder.BuildConfig(target="/tmp/x",
                                     package_keys=["whdload", "sysinfo"])
        self.assertEqual(list(config.leave_out), [])
        asked = dataclasses.replace(config, leave_out=["Tools/SysInfo"])
        self.assertEqual(list(asked.leave_out), ["Tools/SysInfo"])

    def test_no_package_names_a_drawer_in_the_source(self):
        from pistorm_imager.core import packages                 # noqa: PLC0415
        for package in packages.CATALOGUE:
            self.assertFalse(getattr(package, "supersedes", ()),
                             f"{package.key} still carries a hand-written "
                             f"list of drawers to remove")

    def test_the_drawer_and_its_icon_really_go(self):
        #  The unit test asked skip() about files and passed while a card was
        #  built with an empty Tools/SysInfo whose icon was still on the
        #  desktop: copy_volume creates a drawer without consulting skip at
        #  all, and Tools/SysInfo.info is not inside Tools/SysInfo. Both were
        #  found by reading the finished card, so this copies a real volume.
        from pistorm_imager.core import amigaos, compat, rdb     # noqa: PLC0415
        folder = Path(tempfile.mkdtemp(prefix="pistorm-supersede-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        drive = folder / "drive"
        for path in ("Tools/SysInfo/Docs", "Tools/SysInfoExtra", "System/MUI"):
            (drive / path).mkdir(parents=True)
        (drive / "Tools/SysInfo/SysInfo").write_bytes(b"old")
        (drive / "Tools/SysInfo/Docs/guide").write_bytes(b"old")
        (drive / "Tools/SysInfo.info").write_bytes(b"icon")
        (drive / "Tools/SysInfoExtra/keep").write_bytes(b"keep")
        (drive / "System/MUI/muimaster.library").write_bytes(b"keep")

        image = folder / "out.hdf"
        size = 16 * 1024 * 1024
        with open(image, "wb") as handle:
            handle.truncate(size)
        with open(image, "r+b") as handle:
            volume = amigaos.make_volume(handle, 0, size // 512, "Test",
                                         rdb.DOSTYPE_PFS3)
            fixer = compat.Compatibility(Progress())
            fixer.supersede(["Tools/SysInfo"])
            #  A drawer overlay claims its destination the other way, and
            #  that must not stop the drive's own MUI being copied.
            fixer.displace(["System/MUI"])
            amigaos.install_tree(volume, drive, "", Progress(), compat=fixer)
            volume.close()

        from pistorm_imager.core import pfs3                     # noqa: PLC0415
        with open(image, "rb") as handle:
            back = pfs3.Pfs3Volume(handle, 0)
            def there(path):
                return back.find(path) is not None
            self.assertFalse(there("Tools/SysInfo"),
                             "the drawer itself was still created")
            self.assertFalse(there("Tools/SysInfo.info"),
                             "its icon was left on the desktop, opening an "
                             "empty window")
            self.assertFalse(there("Tools/SysInfo/SysInfo"))
            self.assertTrue(there("Tools/SysInfoExtra/keep"),
                            "a drawer with a longer name was taken too")
            self.assertTrue(there("System/MUI/muimaster.library"),
                            "a displaced drawer must still be merged into")

    def test_the_same_through_the_path_a_real_build_uses(self):
        #  A boot drive filled from an .hdf goes through copy_volume, not
        #  install_tree, and that is the path the card was built by. Testing
        #  only the other one is how this shipped.
        from pistorm_imager.core import amigaos, compat, pfs3, rdb  # noqa: PLC0415
        folder = Path(tempfile.mkdtemp(prefix="pistorm-supersede2-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        drive = folder / "drive"
        (drive / "Tools/SysInfo/Docs").mkdir(parents=True)
        (drive / "Tools/SysInfoExtra").mkdir(parents=True)
        (drive / "Tools/SysInfo/SysInfo").write_bytes(b"old")
        (drive / "Tools/SysInfo/Docs/guide").write_bytes(b"old")
        (drive / "Tools/SysInfo.info").write_bytes(b"icon")
        (drive / "Tools/SysInfoExtra/keep").write_bytes(b"keep")

        size = 16 * 1024 * 1024
        source_image = folder / "source.hdf"
        with open(source_image, "wb") as handle:
            handle.truncate(size)
        with open(source_image, "r+b") as handle:
            volume = amigaos.make_volume(handle, 0, size // 512, "Source",
                                         rdb.DOSTYPE_PFS3)
            amigaos.install_tree(volume, drive, "", Progress())
            volume.close()

        target_image = folder / "target.hdf"
        with open(target_image, "wb") as handle:
            handle.truncate(size)
        with open(source_image, "rb") as src, open(target_image, "r+b") as dst:
            reader = pfs3.Pfs3Volume(src, 0)
            volume = amigaos.make_volume(dst, 0, size // 512, "Target",
                                         rdb.DOSTYPE_PFS3)
            fixer = compat.Compatibility(Progress())
            fixer.supersede(["Tools/SysInfo"])
            amigaos.copy_volume(reader, volume, "", Progress(),
                                skip_existing=False, compat=fixer)
            volume.close()

        with open(target_image, "rb") as handle:
            back = pfs3.Pfs3Volume(handle, 0)
            self.assertIsNone(back.find("Tools/SysInfo"))
            self.assertIsNone(back.find("Tools/SysInfo.info"))
            self.assertIsNone(back.find("Tools/SysInfo/Docs/guide"))
            self.assertIsNotNone(back.find("Tools/SysInfoExtra/keep"))


class AHIIsInstalledNotStaged(unittest.TestCase):
    """AHI is a device driver, so it has to be in DEVS: to be any use."""

    def setUp(self):
        from pistorm_imager.core import packages                 # noqa: PLC0415
        self.packages = packages
        self.ahi = packages.CATALOGUE_BY_KEY["ahi"]

    def _lands(self):
        out = {}
        for inside, dest in self.ahi.download.items:
            out[f"{dest}/{Path(inside).name}"] = inside
        for inside, dest, new in self.ahi.download.rename:
            out[f"{dest}/{new}"] = inside
        return out

    def test_the_device_and_its_driver_reach_devs(self):
        landed = self._lands()
        for path in ("Devs/ahi.device", "Devs/AHI/paula.audio",
                     "Devs/AudioModes/PAULA"):
            self.assertIn(path, landed)

    def test_the_prefs_program_lands_under_the_name_its_icon_uses(self):
        #  The archive holds AHI_MUI and AHI_BGUI side by side, and the icon
        #  is called AHI.info - so whichever is chosen has to be renamed or
        #  the icon points at nothing.
        landed = self._lands()
        self.assertIn("Prefs/AHI", landed)
        self.assertIn("Prefs/AHI.info", landed)
        self.assertTrue(landed["Prefs/AHI"].endswith("AHI_MUI"),
                        landed["Prefs/AHI"])

    def test_the_audio_handler_ships_with_its_mountlist(self):
        #  ClassicWB's Startup-Sequence runs
        #  "C:Mount >NIL: DEVS:DOSDrivers/~(#?.info)", so a DOSDriver whose
        #  handler is missing is an error requester on every boot.
        landed = self._lands()
        if "Devs/DOSDrivers/AUDIO" in landed:
            self.assertIn("L/AHI-Handler", landed,
                          "AUDIO: is mounted at boot and needs its handler")

    def test_the_prefs_dependency_is_declared(self):
        #  AHI Prefs is a MUI program. The BGUI alternative would avoid that
        #  and brings a bgui.library carrying floating point instructions,
        #  which on a PiStorm's FPU-less 68040 is guru 8000000B.
        self.assertIn("mui", self.packages.expand(["ahi"]))

    def test_no_floating_point_reaches_this_machine(self):
        #  The rule for any package shipping a binary: count F-line opcodes,
        #  which are the first word of an FPU instruction.
        import struct                                             # noqa: PLC0415
        cache = self.packages.cache_dir() / "ahiusr_4.18.unpacked"
        if not cache.is_dir():
            self.skipTest("the AHI archive is not unpacked on this machine")
        checked = 0
        for inside, _dest in self.ahi.download.items:
            path = cache / inside
            if not path.is_file() or path.suffix in (".info", ".guide"):
                continue
            data = path.read_bytes()
            if data[:4] != b"\x00\x00\x03\xf3":
                continue                      # not an executable
            checked += 1
            fpu = sum(1 for i in range(0, len(data) - 1, 2)
                      if 0xF200 <= struct.unpack_from(">H", data, i)[0]
                      <= 0xF23F)
            with self.subTest(file=inside):
                self.assertEqual(fpu, 0,
                                 f"{inside} has {fpu} floating point "
                                 f"instruction(s); a PiStorm has no FPU")
        self.assertTrue(checked, "no AHI binaries were checked")


class SoftwareTheDriveArrivesWithCanBeLeftOut(unittest.TestCase):
    """A ready-made distribution has its own idea of what belongs on a card.

    ClassicWB FULL carries thirty programs in Programs alone, some obsolete,
    some unfinished, some simply not to taste - and the only choice was all
    of it or none. The exclusion chooser that already existed was built for a
    games drive indexed by letter, and asked of a system drive it returned
    fifty-one groups with nothing in them.
    """

    def _reader(self, tree):
        class Entry:
            def __init__(self, name, is_dir):
                self.name, self.is_dir = name, is_dir
                self.anode = self.block = hash(name) & 0xFFFF
        class Reader:
            def listdir(self, where=None):
                if where is None:
                    return [Entry(n, True) for n in tree]
                for name, kids in tree.items():
                    if (hash(name) & 0xFFFF) == where:
                        return [Entry(k, d) for k, d in kids]
                return []
        return Reader()

    def test_only_the_drawers_software_lives_in_are_offered(self):
        from pistorm_imager.core import content                  # noqa: PLC0415
        found = content.installed_programs(self._reader({
            "Programs": [("FinalWriter", True), ("ReadMe", False)],
            "WBGames": [("Boum", True)],
            #  Not offered: these hold Workbench's own commands, and a list
            #  offering to delete Tools/Commodities is a trap.
            "Tools": [("Commodities", True)],
            "Utilities": [("Catalogs", True)],
            "Libs": [("mui", True)],
            #  On a system drive these are the letter drawers a distribution
            #  makes for a games partition, not games.
            "Games": [("A", True), ("B", True)],
        }))
        self.assertEqual(found, [("Programs", "FinalWriter"),
                                 ("WBGames", "Boum")])

    def test_a_loose_file_is_not_offered(self):
        #  Only drawers: removing one command out of Programs is not worth a
        #  list sixty rows long.
        from pistorm_imager.core import content                  # noqa: PLC0415
        found = content.installed_programs(self._reader({
            "Programs": [("Find", True), ("Find.info", False)]}))
        self.assertEqual(found, [("Programs", "Find")])

    def test_what_was_unticked_is_left_out_whatever_else_is_set(self):
        #  Replacing an older copy is governed by a switch; leaving software
        #  out is not - it was asked for by name.
        from pistorm_imager.core import builder                  # noqa: PLC0415
        for replace in (True, False):
            config = builder.BuildConfig(
                target="/tmp/x", replace_older_software=replace,
                leave_out=["Programs/FinalWriter"])
            self.assertIn("Programs/FinalWriter", config.leave_out)

    def test_it_goes_by_the_route_that_takes_the_icon_too(self):
        #  The same rule that removes a superseded drawer, so the drawer, its
        #  contents and its .info all go - a leftover icon opening an empty
        #  window is what that rule was fixed for.
        from pistorm_imager.core import compat                   # noqa: PLC0415
        fixer = compat.Compatibility(Progress())
        fixer.supersede(["Programs/FinalWriter"])
        for path in ("Programs/FinalWriter",
                     "Programs/FinalWriter/FinalWriter",
                     "Programs/FinalWriter.info"):
            self.assertTrue(fixer.skip(path), path)
        self.assertTrue(fixer.skip_drawer("Programs/FinalWriter"))
        self.assertFalse(fixer.skip("Programs/FinalWriterPro/x"))

    def test_the_real_distribution_offers_what_it_should(self):
        from pistorm_imager.core import amigaos, content         # noqa: PLC0415
        image = Path.home() / "Downloads/ClassicWB_FULL_v28/System.hdf"
        if not image.exists():
            self.skipTest("ClassicWB FULL is not on this machine")
        reader, _label = amigaos.open_amiga_volume(str(image), "")
        try:
            found = content.installed_programs(reader)
        finally:
            try:
                reader.f.close()
            except Exception:                                    # noqa: BLE001
                pass
        drawers = {drawer for drawer, _name in found}
        self.assertEqual(drawers, {"Programs", "WBGames"})
        names = {name for _drawer, name in found}
        self.assertIn("FinalWriter", names)
        self.assertIn("Boum", names)
        for never in ("Commodities", "Catalogs", "A", "B"):
            self.assertNotIn(never, names)


class SoftwareThatCannotWorkIsFlagged(unittest.TestCase):
    """A distribution carries software written for the machine it was built on.

    ClassicWB's FMSsys is the example the user named: MountFMS does
    "assign FMS: A-Programs:FMSsys" and "mount FF0:", and a card built here
    has neither an A-Programs: volume nor a DEVS:DOSDrivers/FF0. So it asks
    a question, fails, and nothing says why. Only what can be shown from the
    files counts - a binary for another processor, a device that will not be
    there, a volume that will not exist.
    """

    def _reader(self, files):
        """A volume made of the paths given, with drawers implied by them."""
        dirs = set()
        for path in files:
            parts = path.split("/")
            for depth in range(1, len(parts)):
                dirs.add("/".join(parts[:depth]))

        class Entry:
            def __init__(self, path, is_dir):
                self.path, self.name = path, path.rpartition("/")[2]
                self.is_dir = is_dir
                self.anode = self.block = abs(hash(path)) & 0xFFFFFF

        by_locator = {}
        def entry(path, is_dir):
            made = Entry(path, is_dir)
            by_locator[made.anode] = path
            return made

        class Reader:
            def listdir(self, where=None):
                parent = "" if where is None else by_locator.get(where, "\0")
                out = []
                for path in sorted(dirs | set(files)):
                    head, _, tail = path.rpartition("/")
                    if head == parent and tail:
                        out.append(entry(path, path in dirs))
                return out
            def find(self, path):
                if path in dirs: return entry(path, True)
                if path in files: return entry(path, False)
                return None
            def read_file(self, e):
                return files.get(getattr(e, "path", ""), b"")
        return Reader()

    def _drive(self, script):
        return self._reader({"Programs/Thing/DoIt": script})

    def test_a_missing_device_is_reported(self):
        from pistorm_imager.core import content                  # noqa: PLC0415
        found = content.cannot_work(self._drive(b"mount FF0:\n"),
                                    volumes=["sys"], dosdrivers=["AUDIO"])
        self.assertEqual(len(found), 1)
        self.assertIn("FF0", found[0].reasons[0])

    def test_a_device_that_is_there_is_not_reported(self):
        from pistorm_imager.core import content                  # noqa: PLC0415
        self.assertEqual(content.cannot_work(self._drive(b"mount FF0:\n"),
                                             volumes=["sys"],
                                             dosdrivers=["FF0"]), [])

    def test_a_missing_volume_is_reported(self):
        from pistorm_imager.core import content                  # noqa: PLC0415
        found = content.cannot_work(
            self._drive(b"assign FMS: A-Programs:FMSsys\n"),
            volumes=["sys", "games"], dosdrivers=[])
        self.assertEqual(len(found), 1)
        self.assertIn("A-Programs", found[0].reasons[0])

    def test_a_volume_this_card_will_have_is_not_reported(self):
        from pistorm_imager.core import content                  # noqa: PLC0415
        self.assertEqual(content.cannot_work(
            self._drive(b"assign X: Games:stuff\n"),
            volumes=["sys", "games"], dosdrivers=[]), [])

    def test_a_binary_for_another_processor_is_reported(self):
        from pistorm_imager.core import content                  # noqa: PLC0415
        found = content.cannot_work(self._drive(b"\x7fELF and the rest"),
                                    volumes=["sys"], dosdrivers=[])
        self.assertEqual(len(found), 1)
        self.assertIn("another processor", found[0].reasons[0])

    def test_documentation_is_not_evidence(self):
        #  A guide explaining how to mount PC: is not a script that tries to,
        #  and quoting one is how a check like this stops being believed.
        from pistorm_imager.core import content                  # noqa: PLC0415
        reader = self._reader(
            {"Programs/Thing/Notes.guide": b"you can mount PC: like so"})
        self.assertEqual(content.cannot_work(reader, ["sys"], []), [])

    def test_a_programs_own_binary_is_left_alone(self):
        from pistorm_imager.core import content                  # noqa: PLC0415
        reader = self._reader(
            {"Programs/Thing/Thing": b"\x00\x00\x03\xf3 a 68k program"})
        self.assertEqual(content.cannot_work(reader, ["sys"], []), [])

    def test_the_drives_own_assigns_count_as_volumes(self):
        #  A distribution makes its own assigns; calling those missing would
        #  condemn most of what it ships.
        from pistorm_imager.core import content                  # noqa: PLC0415
        class Reader:
            def find(self, path):
                class E: name, is_dir = path, False
                return E() if path == "S/User-Startup" else None
            def read_file(self, _e):
                return b"Assign >NIL: A-Programs: SYS:Programs\n"
            def listdir(self, where=None): return []
        found = content.volumes_on_the_card(Reader(), ["DH0", "Games"])
        self.assertIn("a-programs", found)
        self.assertIn("games", found)
        self.assertIn("sys", found)

    def test_it_finds_what_the_user_pointed_at(self):
        from pistorm_imager.core import amigaos, content          # noqa: PLC0415
        image = Path.home() / "Downloads/ClassicWB_FULL_v28/System.hdf"
        if not image.exists():
            self.skipTest("ClassicWB FULL is not on this machine")
        reader, _label = amigaos.open_amiga_volume(str(image), "")
        try:
            volumes = content.volumes_on_the_card(
                reader, ["DH0", "Games", "Demos", "Work"])
            entry = reader.find("Devs/DOSDrivers")
            drivers = ([e.name for e in reader.listdir(content._locator(entry))]
                       if entry is not None else [])
            found = content.cannot_work(reader, volumes, drivers)
        finally:
            try:
                reader.f.close()
            except Exception:                                     # noqa: BLE001
                pass
        drawers = {b.drawer for b in found}
        self.assertIn("Programs/FMSsys", drawers)
        #  And it stays quiet about the thirty-five that are fine.
        self.assertLess(len(drawers), 6, f"too eager: {sorted(drawers)}")



class TheADFHelperIsClickable(unittest.TestCase):
    """The archive ships no Workbench front end at all.

    Its own scripts want a Shell and a filename, and the author's suggestion
    was to drive them from ToolsDaemon or DOpus. So this package writes a
    small script of its own that asks for the file with RequestFile, and
    borrows an icon from the archive to make it double-clickable.
    """

    def test_the_helper_is_clickable_and_named_for_its_icon(self):
        #  Workbench pairs a file with the icon of the same name plus .info.
        #  A script called MountADF beside an icon called anything else is
        #  two things that do nothing, and nothing says so.
        from pistorm_imager.core import packages                 # noqa: PLC0415
        adf = packages.CATALOGUE_BY_KEY["adfdevice"].download
        written = {name: dest for name, dest, _text in adf.write}
        self.assertIn("MountADF", written)
        for _inside, dest, icon, tool in adf.retool:
            self.assertEqual(icon, "MountADF.info")
            self.assertEqual(dest, written["MountADF"],
                             "the icon has to land in the same drawer")
            self.assertEqual(tool, "IconX",
                             "IconX is what runs a script from Workbench")

    def test_the_helper_hands_over_to_the_archives_own_script(self):
        from pistorm_imager.core import packages                 # noqa: PLC0415
        adf = packages.CATALOGUE_BY_KEY["adfdevice"]
        text = next(t for n, _d, t in adf.download.write if n == "MountADF")
        self.assertIn(".key NAME/F", text.splitlines()[0],
                      "without a .key line <NAME> is never substituted")
        self.assertIn("RequestFile", text)
        target = "SYS:Utilities/ADF_Device/Insert.script"
        self.assertIn(target, text)
        landed = {f"{dest}/{Path(src).name}"
                  for src, dest in adf.download.items}
        self.assertIn(target.removeprefix("SYS:"), landed,
                      "the script it calls has to be one this package installs")

    def test_a_retooled_icon_keeps_working_as_an_icon(self):
        #  Rewriting the DefaultTool moves everything after it. Getting that
        #  wrong leaves an icon Workbench cannot read, which looks exactly
        #  like the file having no icon at all.
        from pistorm_imager.core import amigainfo, packages       # noqa: PLC0415
        cache = packages.cache_dir() / "ADF_Device.unpacked"
        if not cache.is_dir():
            self.skipTest("the ADF Device archive is not unpacked here")
        source = cache / "ADF_Device_v1.3/ADF_Device.guide.info"
        original = source.read_bytes()
        before = amigainfo.read_tooltypes(original)
        after = amigainfo.set_default_tool(original, "IconX")
        self.assertEqual(amigainfo.read_default_tool(after), "IconX")
        self.assertEqual(amigainfo.read_tooltypes(after), before,
                         "the tool types moved or were lost")

    def test_setting_the_tool_twice_does_not_grow_the_icon(self):
        from pistorm_imager.core import amigainfo, packages       # noqa: PLC0415
        cache = packages.cache_dir() / "ADF_Device.unpacked"
        if not cache.is_dir():
            self.skipTest("the ADF Device archive is not unpacked here")
        data = (cache / "ADF_Device_v1.3/ADF_Device.guide.info").read_bytes()
        once = amigainfo.set_default_tool(data, "IconX")
        twice = amigainfo.set_default_tool(once, "IconX")
        self.assertEqual(once, twice)

    def test_an_icon_it_cannot_read_is_left_out_not_mangled(self):
        from pistorm_imager.core import amigainfo                 # noqa: PLC0415
        with self.assertRaises(amigainfo.InfoError):
            amigainfo.set_default_tool(b"\x89PNG\r\n\x1a\n" + b"\0" * 200,
                                       "IconX")


class OlderCopiesAreDiscoveredNotDeclared(unittest.TestCase):
    """Which programs exist is never written into this source.

    The candidates used to be a curated tuple on each package - two entries,
    checked by hand against ClassicWB FULL v28 and correct for nothing else.
    Build on another distribution and the feature found nothing and said
    nothing, which is the shape of failure this project keeps meeting.

    The names now come from the archives the chosen packages install, and the
    drive is searched for them.
    """

    def test_the_names_come_from_the_archives(self):
        from pistorm_imager.core import packages                 # noqa: PLC0415
        wanted, filling = packages.principal_programs(["sysinfo"])
        if not wanted:
            self.skipTest("the SysInfo archive is not cached on this machine")
        self.assertIn("sysinfo", wanted)
        key, label, version = wanted["sysinfo"]
        self.assertEqual(key, "sysinfo")
        self.assertIsNotNone(version, "its own version has to be readable")
        self.assertTrue(filling)

    def test_only_principal_programs_are_matched(self):
        #  Matching every file inside a package's tree made a PFS3 tool in
        #  MyFiles look like a duplicate of something buried in Visage, and
        #  the version comparison made it look certain.
        from pistorm_imager.core import packages                 # noqa: PLC0415
        wanted, _filling = packages.principal_programs(["visage"])
        if not wanted:
            self.skipTest("the Visage archive is not cached on this machine")
        self.assertIn("visage", wanted)
        self.assertLess(len(wanted), 8,
                        f"too many names to match safely: {sorted(wanted)}")

    def _reader(self, files):
        class Entry:
            def __init__(self, name):
                self.name, self.is_dir = name, False
        class Reader:
            def walk(self):
                return [(path, Entry(path.rpartition("/")[2]))
                        for path in files]
            def read_file(self, entry):
                return files[[p for p in files
                              if p.endswith("/" + entry.name)][0]]
        return Reader()

    def test_ours_has_to_be_provably_newer_to_be_offered(self):
        from pistorm_imager.core import content                  # noqa: PLC0415
        older = b"\x00\x00\x03\xf3$VER: Thing 1.2 (1993)"
        same = b"\x00\x00\x03\xf3$VER: Thing 4.4 (2020)"
        for body, certain in ((older, True), (same, False)):
            with self.subTest(certain=certain):
                found = content.find_duplicates(
                    self._reader({"Programs/Thing/Thing": body}),
                    {"thing": ("pkg", "Thing", (4, 4))})
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].certain, certain)

    def test_a_copy_with_no_readable_version_is_not_offered(self):
        #  Removing a drawer whole on a name match alone is how ClassicWB's
        #  System/FBlit - the same build as the package, plus an FBlitGUI it
        #  does not ship - would be taken away.
        from pistorm_imager.core import content                  # noqa: PLC0415
        found = content.find_duplicates(
            self._reader({"Programs/Thing/Thing": b"\x00\x00\x03\xf3no version"}),
            {"thing": ("pkg", "Thing", (4, 4))})
        self.assertEqual(found, [])

    def test_a_system_drawer_is_never_the_thing_removed(self):
        #  Reachable only where a program shares its name with the drawer it
        #  sits in - a file called "Tools" inside Tools - but that is exactly
        #  the case where the naming rule would say yes and the answer is no.
        from pistorm_imager.core import content                  # noqa: PLC0415
        body = b"\x00\x00\x03\xf3$VER: Thing 1.0 (1993)"
        for drawer in ("C", "Libs", "S", "Tools", "Devs", "WBStartup"):
            with self.subTest(drawer=drawer):
                self.assertEqual(
                    content.find_duplicates(
                        self._reader({f"{drawer}/{drawer}": body}),
                        {drawer.lower(): ("p", drawer, (4, 4))}),
                    [], f"{drawer} would have been removed whole")

    def test_a_drawer_this_build_fills_is_never_offered(self):
        #  Our MUI overlay merges into the drive's own System/MUI, so every
        #  class in it matches by name and none of them is a duplicate.
        from pistorm_imager.core import content                  # noqa: PLC0415
        body = b"\x00\x00\x03\xf3$VER: Thing 1.0 (1993)"
        files = {"System/MUI/Thing/Thing": body}
        self.assertEqual(
            content.find_duplicates(self._reader(files),
                                    {"thing": ("p", "Thing", (4, 4))},
                                    filling=["System/MUI"]), [])
        self.assertEqual(len(content.find_duplicates(
            self._reader(files), {"thing": ("p", "Thing", (4, 4))})), 1)

    def test_one_row_per_drawer(self):
        #  A drawer named for one program can still hold others the packages
        #  install; the drawer is what goes, so it is named once.
        from pistorm_imager.core import content                  # noqa: PLC0415
        old = b"\x00\x00\x03\xf3$VER: Thing 1.0 (1993)"
        found = content.find_duplicates(
            self._reader({"Programs/Thing/Thing": old,
                          "Programs/Thing/Other": old}),
            {"thing": ("p", "Thing", (4, 4)), "other": ("p", "Other", (4, 4))})
        self.assertEqual([d.drawer for d in found], ["Programs/Thing"])

    def test_the_drawer_has_to_be_named_for_the_program(self):
        #  What goes is the whole drawer, so a program sitting inside
        #  somebody else's is not a duplicate of anything. Without this the
        #  search offered to delete Programs/DiskSalv, because Picasso96
        #  ships an "Installer" and DiskSalv's drawer has one too - and
        #  Tools/Commodities, holding Exchange, Blanker and CrossDOS,
        #  because Commodore's ClickToFront commodity lives in it.
        from pistorm_imager.core import content                  # noqa: PLC0415
        old = b"\x00\x00\x03\xf3$VER: Thing 1.0 (1993)"
        offered = content.find_duplicates(
            self._reader({"Programs/Thing/Thing": old}),
            {"thing": ("p", "Thing", (4, 4))})
        self.assertEqual([d.drawer for d in offered], ["Programs/Thing"])
        for elsewhere in ("Programs/DiskSalv/Thing",
                          "Tools/Commodities/Thing",
                          "Programs/Other/Sub/Thing"):
            with self.subTest(where=elsewhere):
                self.assertEqual(
                    content.find_duplicates(self._reader({elsewhere: old}),
                                            {"thing": ("p", "Thing", (4, 4))}),
                    [], f"{elsewhere} is not a drawer about Thing")

    def test_a_newer_copy_on_the_drive_is_never_offered_by_default(self):
        #  ClassicWB's Scalos is 39.222 against the package's 39.218, so
        #  removing it would be a downgrade.
        from pistorm_imager.core import content                  # noqa: PLC0415
        newer = b"\x00\x00\x03\xf3$VER: Thing 9.9 (2020)"
        found = content.find_duplicates(
            self._reader({"Programs/Thing/Thing": newer}),
            {"thing": ("p", "Thing", (4, 4))})
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].certain)
        self.assertGreater(found[0].theirs, found[0].ours)

    def test_it_finds_what_the_hand_written_list_used_to(self):
        #  The real check: discovery on the real distribution must reach the
        #  same answer the curated entry did, and no more.
        from pistorm_imager.core import amigaos, content, packages  # noqa: PLC0415
        image = Path.home() / "Downloads/ClassicWB_FULL_v28/System.hdf"
        if not image.exists():
            self.skipTest("ClassicWB FULL is not on this machine")
        wanted, filling = packages.principal_programs(
            ["sysinfo", "fblit", "ftext", "freewheel", "visage", "whdload"])
        if not wanted:
            self.skipTest("the archives are not cached on this machine")
        reader, _label = amigaos.open_amiga_volume(str(image), "")
        try:
            found = content.find_duplicates(reader, wanted, filling)
        finally:
            try:
                reader.f.close()
            except Exception:                                    # noqa: BLE001
                pass
        offered = [d.drawer for d in found if d.certain]
        self.assertIn("Tools/SysInfo", offered)
        #  And the ones a name match would have got wrong stay out.
        self.assertNotIn("System/FBlit", [d.drawer for d in found])
        self.assertNotIn("System/FWheel", [d.drawer for d in found])


class NothingShipsABinaryThisMachineCannotRun(unittest.TestCase):
    """AmigaOS 3.1 loads HUNK executables. It cannot load ELF.

    Directory Opus 4 was in the catalogue for a long time as Aminet's
    DirectoryOpus-4.18.22.lha, which is listed "Architecture:
    ppc-amigaos >= 4.0.0" - the AmigaOS 4 PowerPC port. Its DirectoryOpus
    binary begins \\x7fELF, so it was written to every card and could never
    have started, and the entry that replaced ClassicWB's own working 68k
    4.16 with it made the card worse than leaving it alone.

    Nothing on Aminet carries a 68k build of Opus 4: what is there is the
    MorphOS port, the GPL source, catalogs and the manual. So the package is
    gone rather than pointed somewhere hopeful.
    """

    def test_directory_opus_is_not_offered(self):
        from pistorm_imager.core import packages                 # noqa: PLC0415
        self.assertNotIn("diropus4", packages.CATALOGUE_BY_KEY)

    def test_no_elf_binary_reaches_the_card(self):
        #  Archives ship OS4, MorphOS and AROS builds beside the 68k one -
        #  iGame carries iGame.OS4 and iGame.MOS - so the question is not
        #  whether one is in the archive but whether it lands. Every one
        #  found among the files a package installs must be refused by the
        #  compatibility pass.
        from pistorm_imager.core import compat, packages          # noqa: PLC0415
        from pistorm_imager.core.util import Progress             # noqa: PLC0415
        cache = packages.cache_dir()
        fixer = compat.Compatibility(Progress())
        checked = elves = 0
        for package in packages.CATALOGUE:
            url = getattr(package.download, "url", "") or ""
            stem = Path(url).name
            for suffix in (".lha", ".zip", ".run", ".tar.gz"):
                stem = stem.removesuffix(suffix)
            if not stem or not (cache / f"{stem}.unpacked").is_dir():
                continue                    # not fetched on this machine
            for source, dest in packages.overlays_for([package.key],
                                                      progress=Progress()):
                path = Path(source)
                files = ([path] if path.is_file()
                         else [c for c in path.rglob("*") if c.is_file()])
                for item in files:
                    try:
                        data = item.read_bytes()
                    except OSError:
                        continue
                    checked += 1
                    if data[:4] != b"\x7fELF":
                        continue
                    elves += 1
                    landed = f"{dest}/{item.name}"
                    fixer.offer(landed, data)
                    with self.subTest(package=package.key, file=item.name):
                        self.assertTrue(
                            fixer.skip(landed),
                            f"{package.key} would put {item.name} on the card "
                            f"and AmigaOS 3.1 cannot load an ELF binary")
        if not checked:
            self.skipTest("no package archives are cached on this machine")
        self.assertTrue(elves, "no ELF binary was met, so nothing was proved")

    def test_a_68k_binary_is_left_alone(self):
        from pistorm_imager.core import compat                    # noqa: PLC0415
        fixer = compat.Compatibility(Progress())
        hunk = b"\x00\x00\x03\xf3 a real Amiga executable"
        fixer.offer("C/Thing", hunk)
        self.assertFalse(fixer.skip("C/Thing"))


class EachContentDriveGetsItsOwnLauncher(unittest.TestCase):
    """Games and demos are not the same thing and do not share a window.

    One iGame with one repos.prefs covering every drive put four hundred
    games and a few hundred demos in the same list, which is not what either
    is for. Each content drive gets its own installation instead, the way
    PiMiga does it - the first keeps the familiar name, and a Demos drive
    arrives as iDemos beside it.
    """

    def setUp(self):
        from pistorm_imager.core import builder                  # noqa: PLC0415
        self.builder = builder
        self.folder = Path(tempfile.mkdtemp(prefix="pistorm-launcher-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def _config(self, drives, keys=("igame",)):
        specs = [self.builder.AmigaPartitionSpec(name="DH0", bootable=True,
                                                 volume_name="System")]
        for index, (volume, whdload) in enumerate(drives, start=1):
            where = self.folder / volume
            (where / "WHDLoad").mkdir(parents=True) if whdload else where.mkdir()
            specs.append(self.builder.AmigaPartitionSpec(
                name=f"DH{index}", volume_name=volume,
                content_folder=str(where)))
        return self.builder.BuildConfig(target="/tmp/x", package_keys=list(keys),
                                        amiga_partitions=specs)

    def _instances(self, config, pairs=()):
        return self.builder._igame_instances(config, Progress(), list(pairs))

    def test_a_second_drive_gets_a_launcher_of_its_own(self):
        made = self._instances(self._config([("Games", True), ("Demos", True)]),
                               [("/tmp/tree", "Programs/iGame")])
        where = {dest for _src, dest in made}
        self.assertEqual(where, {"Programs/iGame", "Programs/iDemos"})

    def test_each_scans_only_its_own_drive(self):
        made = self._instances(self._config([("Games", True), ("Demos", True)]))
        scans = {dest: Path(src).read_text().strip()
                 for src, dest in made if src.endswith("repos.prefs")}
        self.assertEqual(scans["Programs/iGame"], "Games:WHDLoad")
        self.assertEqual(scans["Programs/iDemos"], "Demos:WHDLoad")

    def test_the_second_installation_is_the_whole_program(self):
        #  A repos.prefs on its own is a launcher with nothing to launch it.
        pairs = [("/tmp/iGame-v2.6.1", "Programs/iGame"),
                 ("/tmp/iGame", "Programs/iGame")]
        made = self._instances(self._config([("Games", True), ("Demos", True)]),
                               pairs)
        copied = [src for src, dest in made if dest == "Programs/iDemos"]
        self.assertEqual(len(copied), len(pairs) + 1,
                         f"the program itself was not copied: {copied}")

    def test_one_drive_still_gets_just_iGame(self):
        made = self._instances(self._config([("Games", True)]),
                               [("/tmp/tree", "Programs/iGame")])
        self.assertEqual({dest for _src, dest in made}, {"Programs/iGame"})

    def test_a_drawer_that_is_not_there_is_not_named(self):
        #  Pointing a launcher at a WHDLoad drawer that does not exist is how
        #  a donor's own list behaved.
        made = self._instances(self._config([("Music", False)]))
        text = next(Path(src).read_text().strip() for src, _d in made)
        self.assertEqual(text, "Music:")

    def test_nothing_is_made_without_the_package(self):
        self.assertEqual(self._instances(
            self._config([("Games", True)], keys=("whdload",))), [])

    def test_the_names_come_from_the_partitions(self):
        #  Not from anything written down here: a drive the user calls
        #  Cracktros gets iCracktros.
        made = self._instances(
            self._config([("Games", True), ("Cracktros", True)]),
            [("/tmp/tree", "Programs/iGame")])
        self.assertIn("Programs/iCracktros", {dest for _src, dest in made})
