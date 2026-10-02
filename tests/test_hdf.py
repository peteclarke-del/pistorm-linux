"""Tests for hard disk image import/export and the PiStorm compatibility repair."""
import dataclasses
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import (amigaos, builder, hdfcheck, mbr,  # noqa: E402
                                  packages, presets, rdb)
from pistorm_imager.core.util import MIB, Progress  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from emu68_stub import EMU68  # noqa: E402

QUIET = Progress()
#  The handler embedded in the RDB where one has been cached; these tests are
#  about what goes onto the drives, and build just as well without it, as a
#  clean machine has to.
PFS3_HANDLER = str(presets.cached_pfs3_handler() or "")


class _Scratch(unittest.TestCase):
    """Base class giving each test a temporary directory that is cleaned up.

    The full-build tests each write a multi-gigabyte sparse card image; left
    behind, a few runs quietly fill /tmp.
    """

    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-test-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder



def make_hdf(path: Path, size: int, partitions, *, cylinders=None,
             filesystems=None) -> rdb.Rdb:
    geometry = rdb.Geometry()
    table = rdb.Rdb(geometry=geometry, partitions=partitions,
                    filesystems=filesystems or [],
                    cylinders=cylinders or (size // 512) // geometry.cyl_blocks)
    with open(path, "wb") as handle:
        handle.truncate(size)
        table.write(handle, 0)
    return table


class TestCompatibilityChecks(_Scratch):
    def setUp(self):
        self.geometry = rdb.Geometry()
        self.capacity = 200 * MIB

    def analyse(self, partitions, filesystems=None):
        table = rdb.Rdb(geometry=self.geometry, partitions=partitions,
                        filesystems=filesystems or [], cylinders=200)
        return table, hdfcheck.analyse(table, self.capacity)

    def codes(self, findings):
        return {f.code for f in findings}

    def test_clean_image_reports_nothing(self):
        _t, findings = self.analyse([rdb.Partition("DH0", 1, 199,
                                                   rdb.DOSTYPE_FFS_INTL,
                                                   bootable=True)])
        self.assertEqual(findings, [])

    def test_detects_dangerous_max_transfer(self):
        _t, findings = self.analyse([rdb.Partition(
            "DH0", 1, 199, rdb.DOSTYPE_FFS_INTL, bootable=True,
            max_transfer=0xFFFFFF)])
        self.assertIn("max_transfer", self.codes(findings))

    def test_detects_missing_filesystem_handler(self):
        _t, findings = self.analyse([rdb.Partition("DH0", 1, 199,
                                                   rdb.DOSTYPE_PFS3,
                                                   bootable=True)])
        self.assertIn("missing_handler", self.codes(findings))

    def test_embedded_handler_satisfies_the_check(self):
        _t, findings = self.analyse(
            [rdb.Partition("DH0", 1, 199, rdb.DOSTYPE_PFS3, bootable=True)],
            [rdb.FileSystem(rdb.DOSTYPE_PFS3, b"handler")])
        self.assertNotIn("missing_handler", self.codes(findings))

    def test_detects_overlap_and_will_not_fix_it(self):
        _t, findings = self.analyse([
            rdb.Partition("DH0", 1, 100, rdb.DOSTYPE_FFS_INTL, bootable=True),
            rdb.Partition("DH1", 50, 150, rdb.DOSTYPE_FFS_INTL)])
        overlap = [f for f in findings if f.code == "overlap"]
        self.assertTrue(overlap)
        self.assertFalse(overlap[0].fixable)
        self.assertTrue(hdfcheck.blocking(findings))

    def test_detects_partition_past_the_end(self):
        _t, findings = self.analyse([rdb.Partition("DH0", 1, 999,
                                                   rdb.DOSTYPE_FFS_INTL,
                                                   bootable=True)])
        self.assertIn("past_end", self.codes(findings))

    def test_detects_duplicate_names_and_missing_boot_flag(self):
        _t, findings = self.analyse([
            rdb.Partition("DH0", 1, 100, rdb.DOSTYPE_FFS_INTL, bootable=False),
            rdb.Partition("DH0", 101, 199, rdb.DOSTYPE_FFS_INTL, bootable=False)])
        self.assertIn("duplicate_name", self.codes(findings))
        self.assertIn("not_bootable", self.codes(findings))

    def test_repair_fixes_what_it_reports(self):
        table, findings = self.analyse([
            rdb.Partition("DH0", 1, 100, rdb.DOSTYPE_FFS_INTL,
                          max_transfer=0xFFFFFF, mask=0xFFFFFFFF),
            rdb.Partition("DH0", 101, 199, rdb.DOSTYPE_FFS_INTL,
                          sectors_per_block=4)])
        self.assertTrue(findings)
        hdfcheck.repair(table, self.capacity)
        self.assertEqual(hdfcheck.analyse(table, self.capacity), [])

    def test_pfs3_handler_can_come_from_a_pds3_donor(self):
        """PFS3 and PDS3 are the same handler; a donor of either satisfies both."""
        table, _f = self.analyse([rdb.Partition("DH0", 1, 199, rdb.DOSTYPE_PFS3,
                                                bootable=True)])
        donor = [rdb.FileSystem(rdb.DOSTYPE_PDS3, b"\x00\x00\x03\xf3" + b"x" * 500)]
        actions = hdfcheck.repair(table, self.capacity, donor)
        self.assertTrue(any("relabelled" in a for a in actions), actions)
        self.assertEqual(hdfcheck.analyse(table, self.capacity), [])
        self.assertEqual(table.filesystems[0].dostype, rdb.DOSTYPE_PFS3)

    def test_fixable_error_blocks_only_after_repair(self):
        """"Fixable" means a repair exists, never that one succeeded."""
        table = rdb.Rdb(geometry=rdb.Geometry(heads=0, sectors=0),
                        partitions=[rdb.Partition("DH0", 1, 199,
                                                  rdb.DOSTYPE_FFS_INTL)],
                        cylinders=200)
        findings = hdfcheck.analyse(table, self.capacity)
        self.assertEqual(hdfcheck.blocking(findings), [],
                         "a fixable fault is not blocking before repair")
        self.assertTrue(hdfcheck.blocking(findings, after_repair=True),
                        "the same fault blocks if it is still there afterwards")

    def test_missing_handler_is_reported_but_does_not_block_the_write(self):
        """The drive is intact; the user just has to supply the handler."""
        _t, findings = self.analyse([rdb.Partition("DH0", 1, 199,
                                                   rdb.DOSTYPE_PFS3,
                                                   bootable=True)])
        self.assertIn("missing_handler", self.codes(findings))
        self.assertEqual(hdfcheck.blocking(findings, after_repair=True), [])
        self.assertTrue(hdfcheck.unresolved(findings))


class TestOverlaysGoThroughTheCompatibilityPass(_Scratch):
    """The rules have to fire on the paths the build actually produces.

    Both of these were written and tested by calling the pass directly with a
    full path - "S/WHDLoad.prefs", "Programs/iGame/gameslist.csv" - which is
    not what a copy hands it.  The unit tests passed and the card came out
    unchanged.
    """

    UAE_PREFS = (b"ExecuteStartup=uae-configuration cachesize 0\n"
                 b"ExecuteCleanup=uae-configuration cpu_speed max\n"
                 b"QuitKey=$59\n")

    def build(self, overlay_dir, exclude=None):
        out = self.scratch() / "card.hdf"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(out), output_hdf=True,
            image_size=200 * MIB, emu68_prepared_dir=EMU68, fix_compatibility=True,
            pfs3_binary=PFS3_HANDLER,
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0, volume_name="Sys",
                content_folder=str(overlay_dir),
                exclude=list(exclude or []))]), QUIET)
        return out

    def read(self, image, path):
        from pistorm_imager.core import amigaos           # noqa: PLC0415
        volume, _label = amigaos.open_amiga_volume(image)
        entry = volume.find(path)
        return volume.read_file(entry).decode("latin-1") if entry else ""

    UAE_PREFS_PAL = (b";WHDLoad preferences\n"
                     b"PAL           ;force PAL video mode\n"
                     b"QuitKey=$59   ;rawkey code to quit\n"
                     b"ExecuteStartup=uae-configuration cachesize 0\n"
                     b";NTSC         ;already commented, leave alone\n")

    def test_a_forced_display_mode_is_taken_out(self):
        """This killed every game on every card the tool has ever built.

        A donor's WHDLoad preferences carry "PAL", asking WHDLoad to force
        that mode before handing over.  On a PiStorm the machine dies on the
        spot - a yellow screen, which is a CPU exception with no OS left to
        draw a Guru, then black.  Not one game ran.  The same game runs from
        Commodore's own floppy, and runs off this card the moment the line
        is gone.
        """
        source = self.scratch() / "tree"
        (source / "S").mkdir(parents=True)
        (source / "S" / "WHDLoad.prefs").write_bytes(self.UAE_PREFS_PAL)
        body = self.read(self.build(source), "S/WHDLoad.prefs")
        live = [l.strip() for l in body.splitlines()
                if l.strip() and not l.strip().startswith(";")]
        self.assertFalse([l for l in live if l.lower().startswith("pal")],
                         f"PAL is still in force: {live}")
        self.assertTrue([l for l in live if l.startswith("QuitKey")],
                        "the rest of the file should be left alone")

    def test_an_already_commented_mode_is_not_commented_twice(self):
        source = self.scratch() / "tree2"
        (source / "S").mkdir(parents=True)
        (source / "S" / "WHDLoad.prefs").write_bytes(self.UAE_PREFS_PAL)
        body = self.read(self.build(source), "S/WHDLoad.prefs")
        self.assertNotIn(";;NTSC", body)

    def test_whdload_prefs_are_cleaned_when_copied_as_a_tree(self):
        source = self.scratch() / "tree"
        (source / "S").mkdir(parents=True)
        (source / "S" / "WHDLoad.prefs").write_bytes(self.UAE_PREFS)
        body = self.read(self.build(source), "S/WHDLoad.prefs")
        self.assertIn(";ExecuteStartup=uae-configuration", body)
        self.assertIn(";ExecuteCleanup=uae-configuration", body)
        self.assertIn("QuitKey=$59", body)

    def test_the_games_list_is_left_off_when_copied_as_a_tree(self):
        """Another program's data, written on somebody else's machine: iGame
        builds its own from the card, which cannot name a game that is not
        there."""
        source = self.scratch() / "tree"
        games = source / "WHDLOAD" / "OCS" / "Driller"
        games.mkdir(parents=True)
        (games / "Driller.slave").write_bytes(b"x")
        igame = source / "Programs" / "iGame"
        igame.mkdir(parents=True)
        (igame / "gameslist.csv").write_text(
            "0;Driller;x;Sys:WHDLOAD/OCS/Driller/Driller.slave;0;0;0;0\n")
        (igame / "iGame").write_bytes(b"program")
        volume = self.build(source)
        self.assertEqual(self.read(volume, "Programs/iGame/gameslist.csv"), "")
        #  The program itself is still copied.
        self.assertEqual(self.read(volume, "Programs/iGame/iGame"), "program")

    def test_the_repository_list_is_filtered_too(self):
        """Filtering the games list was only half of the job.

        iGame also keeps the list of drawers it scans, and that still named
        every collection the donor had, so a card with the AGA games left out
        still sent iGame looking through a drawer that is not on it.
        """
        source = self.scratch() / "tree"
        for category in ("OCS", "AGA"):
            (source / "WHDLOAD" / category).mkdir(parents=True)
        igame = source / "Programs" / "iGame"
        igame.mkdir(parents=True)
        (igame / "repos.prefs").write_text(
            "Sys:WHDLOAD/OCS/\n"
            "Sys:WHDLOAD/AGA/\n"
            "Sys:WHDLOAD/Nowhere/\n")
        body = self.read(self.build(source, exclude=["WHDLOAD/AGA"]),
                         "Programs/iGame/repos.prefs")
        self.assertIn("WHDLOAD/OCS/", body)
        self.assertNotIn("WHDLOAD/AGA/", body)
        self.assertNotIn("Nowhere", body)


class TestUserStartupOnBothFileSystems(_Scratch):
    """The generated S:User-Startup has to work on either system drive.

    The lookup used to write it existed only on the PFS3 writer, so a build
    onto an FFS system drive got all the way through installing Workbench and
    every package before failing at the last step.
    """

    ADFS = Path(__file__).resolve().parent.parent / "samples" / "workbench"

    def build_with_startup(self, dostype: str) -> Path:
        out = self.scratch() / f"{dostype}.hdf"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(out), output_hdf=True,
            image_size=400 * MIB, emu68_prepared_dir=EMU68,
            install_amigaos=True, adf_folder=str(self.ADFS),
            amiga_volume_name="Workbench", fix_compatibility=False,
            pfs3_binary=PFS3_HANDLER,
            #  fblit contributes a line to S:User-Startup, which is what
            #  makes that file get written at all; iconlib goes into
            #  S:Startup-Sequence instead, so both routes are covered here.
            package_keys=["fblit", "iconlib"],
            package_chipset="OCS", package_display="native",
            amiga_partitions=[
                builder.AmigaPartitionSpec("DH0", None, dostype, True, 0,
                                           volume_name="Workbench")],
        ), QUIET)
        return out

    def check(self, dostype: str) -> None:
        from pistorm_imager.core import amigaos          # noqa: PLC0415
        out = self.build_with_startup(dostype)
        self.assertEqual(builder.list_drives(out)[0].volume, "Workbench")
        volume, _label = amigaos.open_amiga_volume(out)
        entry = volume.find("S/User-Startup")
        self.assertIsNotNone(entry, f"{dostype}: no S:User-Startup written")
        body = volume.read_file(entry).decode("latin-1")
        self.assertIn("FBlit", body)

        #  icon.library cannot be soft-kicked from User-Startup - IPrefs has
        #  already opened the ROM one - so it goes in above IPrefs instead.
        startup = volume.find("S/Startup-Sequence")
        self.assertIsNotNone(startup, f"{dostype}: no S:Startup-Sequence")
        boot = volume.read_file(startup).decode("latin-1")
        lines = [line.strip() for line in boot.splitlines()]
        kick = next(i for i, line in enumerate(lines) if "LoadModule" in line)
        iprefs = next(i for i, line in enumerate(lines)
                      if line.lower().startswith("c:iprefs"))
        self.assertLess(kick, iprefs,
                        "the replacement must load before IPrefs opens the "
                        "ROM icon.library")
        volume.f.close()

    @unittest.skipUnless(ADFS.is_dir(), "no Workbench disks available")
    def test_ffs_system_drive(self):
        self.check("FFS-INTL")

    @unittest.skipUnless(ADFS.is_dir(), "no Workbench disks available")
    def test_pfs3_system_drive(self):
        self.check("PFS3")


class SoftwareKeptOnItsOwnDrive(_Scratch):
    """Programs on a drive of their own, and AmigaOS's parts where it looks.

    The packages are faked with files of the shapes the catalogue's own
    produce, so this is the build itself and nothing downloaded.
    """

    def setUp(self):
        here = self.scratch()
        system = here / "system"
        (system / "S").mkdir(parents=True)
        (system / "S" / "Startup-Sequence").write_bytes(b"LoadWB\n")
        program = here / "AmiTimeKeeper"
        program.mkdir()
        (program / "TimeKeeper").write_bytes(b"\0\0\x03\xf3 a program")
        (program / "Go").write_bytes(
            b"Run SYS:Utilities/AmiTimeKeeper/TimeKeeper\n")
        library = here / "amisslmaster.library"
        library.write_bytes(b"\0\0\x03\xf3 a library")
        #  AWeb's shape: its own drawer inside one other software shares,
        #  and the drawer's icon beside it.
        aweb = here / "AWeb_APL"
        aweb.mkdir()
        (aweb / "AWeb").write_bytes(b"\0\0\x03\xf3 a browser")
        aweb_icon = here / "AWeb_APL.info"
        aweb_icon.write_bytes(b"\xe3\x10 an icon")
        #  AmiSSL's: files straight into a drawer only it uses.
        ssl = here / "OpenSSL"
        ssl.write_bytes(b"\0\0\x03\xf3 a command")
        self.faked = [("amitimekeeper",
                       [(str(program), "Utilities/AmiTimeKeeper"),
                        (str(library), "Libs")]),
                      ("aweb", [(str(aweb), "Programs/AWeb_APL"),
                                (str(aweb_icon), "Programs")]),
                      ("amissl", [(str(ssl), "AmiSSL")])]
        self.out = here / "card.hdf"
        self.config = builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(self.out),
            output_hdf=True, image_size=300 * MIB,
            emu68_prepared_dir=EMU68, pfs3_binary=PFS3_HANDLER,
            package_keys=["amitimekeeper", "aweb", "amissl"],
            package_chipset="AGA", package_display="native",
            amiga_partitions=[
                builder.AmigaPartitionSpec(
                    "DH0", 100 * MIB, "FFS-INTL", True, 0,
                    content_folder=str(system), volume_name="System"),
                builder.AmigaPartitionSpec(
                    "DH1", None, "FFS-INTL", False, -128,
                    volume_name="Programs",
                    software=[packages.Category.NETWORK.value]),
            ])

    def faking(self):
        return unittest.mock.patch.object(packages, "overlays_by_package",
                                          return_value=self.faked)

    def read(self, drive: str, path: str) -> bytes | None:
        volume, _label = amigaos.open_amiga_volume(self.out, drive)
        try:
            entry = volume.find(path)
            return volume.read_file(entry) if entry is not None else None
        finally:
            volume.f.close()

    def test_the_program_goes_on_its_drive_and_its_library_into_libs(self):
        with self.faking():
            builder.run_build(self.config, QUIET)
        program = "Utilities/AmiTimeKeeper/TimeKeeper"
        self.assertIsNotNone(self.read("DH1", program))
        self.assertIsNone(self.read("DH0", program))
        self.assertIsNotNone(self.read("DH1", "Programs/AWeb_APL/AWeb"))
        self.assertIsNotNone(self.read("DH0", "Libs/amisslmaster.library"))
        self.assertIsNone(self.read("DH1", "Libs/amisslmaster.library"))
        #  Every path to it names the drive it is on.
        self.assertIn(b"Run DH1:Utilities/AmiTimeKeeper/TimeKeeper",
                      self.read("DH1", "Utilities/AmiTimeKeeper/Go"))
        startup = self.read("DH0", "S/User-Startup").decode("latin-1")
        self.assertIn(f"Run >NIL: DH1:{program}", startup)
        self.assertIn("AWEB_APL: DH1:Programs/AWeb_APL", startup)
        self.assertIn("Assign AmiSSL: DH1:AmiSSL", startup)
        self.assertIsNotNone(self.read("DH1", "Programs/AWeb_APL.info"))
        self.assertNotIn("SYS:Utilities/AmiTimeKeeper", startup)
        record = self.read("DH0", builder.MANIFEST_PATH).decode("latin-1")
        self.assertIn("DH1:Utilities/AmiTimeKeeper", record)

    def test_only_what_was_moved_is_repointed(self):
        """AWeb shares Programs with software nobody moved."""
        with self.faking():
            moves = builder._software(self.config, QUIET).moves
        self.assertEqual(moves["Programs/AWeb_APL"], "DH1")
        self.assertEqual(moves["AmiSSL"], "DH1")
        #  Shared with other software, so never moved whole.
        self.assertNotIn("Programs", moves)
        self.assertNotIn("Utilities", moves)
        self.assertNotIn("Libs/amisslmaster.library", moves)

    def test_without_a_drive_for_it_everything_stays(self):
        config = dataclasses.replace(self.config, amiga_partitions=[
            dataclasses.replace(spec, software=[])
            for spec in self.config.amiga_partitions])
        with self.faking():
            software = builder._software(config, QUIET)
        self.assertEqual(software.away, {})
        self.assertEqual(software.moves, {})


class AWorkbenchDriveThatReachesPast4GB(_Scratch):
    """Workbench 3.1 on an A600's IDE port, given the driver that reaches.

    The driver and LoadModule are faked with files of their shapes; FS-UAE
    booted the real ones on Kickstart 3.1 and mounted PFS3 drives to 16 GB.
    """

    ADFS = Path(__file__).resolve().parent.parent / "samples" / "workbench"

    @unittest.skipUnless(ADFS.is_dir(), "no Workbench disks available")
    def test_the_driver_loadmodule_and_its_line_are_on_the_drive(self):
        from pistorm_imager.core import util            # noqa: PLC0415
        here = self.scratch()
        driver = here / "A1200" / "scsi.device"
        driver.parent.mkdir()
        driver.write_bytes(b"\0\0\x03\xf3 the driver")
        loader = here / "LoadModule"
        loader.write_bytes(b"\0\0\x03\xf3 LoadModule")
        out = here / "cf.hdf"
        config = builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(out), output_hdf=True,
            amiga_only=True, install_emu68=False, image_size=6 * util.GIB,
            machine_key="a600", accelerator="accelerator",
            install_amigaos=True, adf_folder=str(self.ADFS),
            fix_compatibility=False, pfs3_binary=PFS3_HANDLER,
            amiga_partitions=[
                builder.AmigaPartitionSpec("DH0", 200 * MIB, "FFS-INTL",
                                           True, 0),
                builder.AmigaPartitionSpec("DH1", None, "PFS3", False,
                                           -128)])
        with unittest.mock.patch.object(
                presets, "find_ide_drivers",
                return_value={"A1200": driver, "A600": driver}), \
                unittest.mock.patch.object(
                    packages, "overlays_by_package",
                    return_value=[("loadmodule", [(str(loader), "C")])]):
            self.assertEqual(config.validate(), [])
            builder.run_build(config, QUIET)
        volume, _label = amigaos.open_amiga_volume(out, "DH0")
        try:
            for path in ("Devs/A600/scsi.device", "Devs/A1200/scsi.device",
                         "C/LoadModule"):
                self.assertIsNotNone(volume.find(path), path)
            boot = volume.read_file(volume.find("S/Startup-Sequence")
                                    ).decode("latin-1").lower()
        finally:
            volume.f.close()
        self.assertLess(boot.index("c:loadmodule auto"),
                        boot.index("c:iprefs"),
                        "loaded before Workbench starts")


class TestEmptyPartitionsAreFormatted(_Scratch):
    """A drive with nothing to put in it should still mount."""

    def build(self, partitions):
        out = self.scratch() / "card.hdf"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(out), output_hdf=True,
            image_size=300 * MIB, emu68_prepared_dir=EMU68,
            pfs3_binary=PFS3_HANDLER,
            amiga_partitions=partitions), QUIET)
        return out

    def test_an_empty_drive_is_formatted_and_named(self):
        """Left raw, AmigaOS shows it as NDOS and it has to be formatted by
        hand - which for PFS3 means the handler has to be running first."""
        out = self.build([
            builder.AmigaPartitionSpec("DH0", 100 * MIB, "FFS-INTL", True, 0,
                                       volume_name="Boot"),
            builder.AmigaPartitionSpec("DH1", 80 * MIB, "PFS3", False, -128,
                                       volume_name="Apps"),
            builder.AmigaPartitionSpec("DH2", None, "PFS3", False, -128,
                                       volume_name="Work"),
        ])
        drives = builder.list_drives(out)
        self.assertEqual([d.volume for d in drives], ["Boot", "Apps", "Work"])

    def test_a_drive_falls_back_to_its_device_name(self):
        out = self.build([
            builder.AmigaPartitionSpec("DH0", None, "PFS3", True, 0),
        ])
        self.assertEqual(builder.list_drives(out)[0].volume, "DH0")


class TestListDrives(_Scratch):
    """What a partition's drive chooser is offered."""

    def test_lists_every_drive_with_its_name_and_size(self):
        path = self.scratch() / "two.hdf"
        make_hdf(path, 100 * MIB, [
            rdb.Partition("DH0", 1, 40, rdb.DOSTYPE_FFS_INTL, bootable=True),
            rdb.Partition("DH1", 41, 80, rdb.DOSTYPE_PFS3, bootable=False),
        ])
        drives = builder.list_drives(path)
        self.assertEqual([d.name for d in drives], ["DH0", "DH1"])
        self.assertTrue(drives[0].bootable)
        self.assertFalse(drives[1].bootable)
        self.assertEqual(drives[1].filesystem, "PFS3")
        self.assertGreater(drives[0].size, 0)
        #  The label is what the chooser shows, so it has to name the drive.
        self.assertIn("DH0", drives[0].label)
        self.assertIn("FFS", drives[0].label)

    def test_an_unformatted_drive_still_appears(self):
        """A drive with no file system on it is still a drive you can pick."""
        path = self.scratch() / "raw.hdf"
        make_hdf(path, 100 * MIB,
                 [rdb.Partition("DH0", 1, 80, rdb.DOSTYPE_FFS_INTL)])
        drives = builder.list_drives(path)
        self.assertEqual(len(drives), 1)
        self.assertEqual(drives[0].volume, "")

    def test_an_ffs_drive_inside_an_image_reports_its_label(self):
        """FFS keeps its root block in the middle of the partition.

        Sizing that from the file rather than the partition lands on the wrong
        block, and a perfectly good Workbench reads back as an empty volume.
        """
        from pistorm_imager.core import amigaos, amigafs      # noqa: PLC0415
        path = self.scratch() / "card.hdf"
        table = make_hdf(path, 40 * MIB, [
            rdb.Partition("DH0", 1, 10, rdb.DOSTYPE_FFS_INTL, bootable=True),
            rdb.Partition("DH1", 11, 30, rdb.DOSTYPE_FFS_INTL, bootable=False),
        ])
        with open(path, "r+b") as handle:
            for part, label in ((table.partitions[0], "Workbench"),
                                (table.partitions[1], "Extras")):
                volume = amigaos.make_volume(
                    handle, part.byte_offset(table.geometry),
                    part.blocks(table.geometry), label,
                    amigafs.DOSTYPE_FFS_INTL)
                volume.close()
        drives = builder.list_drives(path)
        self.assertEqual([d.volume for d in drives], ["Workbench", "Extras"])
        self.assertIn('"Workbench"', drives[0].label)

    def test_a_bare_file_system_is_offered_as_the_whole_image(self):
        """ClassicWB and plenty of older .hdf files have no partition table.

        Answering "nothing here" for those read, in the chooser, as though no
        image had been selected at all.
        """
        from pistorm_imager.core import amigaos, amigafs      # noqa: PLC0415
        path = self.scratch() / "bare.hdf"
        with open(path, "w+b") as handle:
            handle.truncate(4 * MIB)
            volume = amigaos.make_volume(handle, 0, (4 * MIB) // amigafs.BLOCK,
                                         "Bare", amigafs.DOSTYPE_FFS_INTL)
            volume.close()
        drives = builder.list_drives(path)
        self.assertEqual(len(drives), 1)
        self.assertTrue(drives[0].whole_image)
        self.assertEqual(drives[0].name, "")
        self.assertEqual(drives[0].volume, "Bare")
        self.assertIn("whole image", drives[0].label)

    def test_a_pimiga_download_is_named_for_what_it_is(self):
        """The file everyone reaches for first holds no Amiga drive at all.

        PiMiga is a Raspberry Pi system running an emulator; its Amiga drives
        are folders inside its Linux root.  Saying only "no Amiga drive found"
        left the user with nothing to act on.
        """
        path = self.scratch() / "pimiga.img"
        with open(path, "w+b") as handle:
            handle.truncate(8 * MIB)
            table = [mbr.MbrPartition(0, 0, 0x0c, 8192, 2048),
                     mbr.MbrPartition(1, 0, 0x83, 10240, 4096)]
            mbr.write_table(handle, table)
        self.assertEqual(builder.list_drives(path), [])
        why = builder.why_no_drives(path)
        self.assertIn("Linux", why)
        self.assertIn("folders", why)

    def test_an_amiga_image_needs_no_explanation(self):
        path = self.scratch() / "ok.hdf"
        make_hdf(path, 100 * MIB,
                 [rdb.Partition("DH0", 1, 80, rdb.DOSTYPE_FFS_INTL)])
        self.assertEqual(builder.why_no_drives(path), "")

    def test_nothing_to_list_without_an_amiga_file_system(self):
        path = self.scratch() / "empty.hdf"
        path.write_bytes(b"\0" * 8192)
        self.assertEqual(builder.list_drives(path), [])
        self.assertEqual(builder.list_drives(self.scratch() / "missing.hdf"), [])


class TestFindRdb(_Scratch):
    def test_finds_an_rdb_in_a_bare_hdf(self):
        folder = self.scratch()
        path = folder / "disk.hdf"
        make_hdf(path, 100 * MIB, [rdb.Partition("DH0", 1, 99,
                                                 rdb.DOSTYPE_FFS_INTL,
                                                 bootable=True)])
        with open(path, "rb") as handle:
            found = builder.find_rdb(handle)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], 0)
        self.assertEqual(found[1].partitions[0].drive_name, "DH0")

    def test_finds_an_rdb_inside_a_card_image(self):
        folder = self.scratch()
        card = folder / "card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(card),
            image_size=300 * MIB, boot_size=96 * MIB, emu68_prepared_dir=EMU68,
            amiga_partitions=[builder.AmigaPartitionSpec("DH0", None,
                                                         "FFS-INTL", True, 0)]),
            QUIET)
        with open(card, "rb") as handle:
            found = builder.find_rdb(handle)
        self.assertIsNotNone(found)
        self.assertGreater(found[0], 0, "should be at the 0x76 partition, not block 0")
        self.assertEqual(found[1].partitions[0].drive_name, "DH0")


class AnImageSaysWhatItIs(_Scratch):
    """One task writes any image; the file decides how.

    A whole card is written as it is, a drive gets a boot partition built
    around it - and both turn up as .img files, compressed or not.
    """

    def test_a_drive_image_is_a_drive(self):
        path = self.scratch() / "disk.img"
        make_hdf(path, 20 * MIB, [rdb.Partition("DH0", 1, 19,
                                                rdb.DOSTYPE_FFS_INTL)])
        self.assertIs(builder.image_kind(path), builder.ImageKind.DRIVE)

    def test_a_bare_file_system_is_a_drive(self):
        path = self.scratch() / "partition.hdf"
        path.write_bytes(b"DOS\x03" + bytes(4 * MIB))
        self.assertIs(builder.image_kind(path), builder.ImageKind.DRIVE)

    def test_a_card_is_a_card_even_compressed(self):
        folder = self.scratch()
        card = folder / "card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(card),
            image_size=300 * MIB, boot_size=96 * MIB, emu68_prepared_dir=EMU68,
            amiga_partitions=[builder.AmigaPartitionSpec("DH0", None,
                                                         "FFS-INTL", True, 0)]),
            QUIET)
        self.assertIs(builder.image_kind(card), builder.ImageKind.CARD)
        import gzip
        packed = folder / "card.img.gz"
        with open(card, "rb") as raw, gzip.open(packed, "wb") as out:
            out.write(raw.read(MIB))
        self.assertIs(builder.image_kind(packed), builder.ImageKind.CARD)

    def test_anything_else_is_neither(self):
        path = self.scratch() / "notes.img"
        path.write_bytes(bytes(MIB))
        self.assertIsNone(builder.image_kind(path))


class TheHandlerKickstartLoads(_Scratch):
    """The file system handler in the RDB, as Kickstart's own loader reads it.

    Every LoadSegBlock said it held 128 longs, so the last one handed the ROM
    its zero padding after the handler's final HUNK_END. The ROM took that
    for another hunk and gave up: the handler never loaded, a CF card booted
    through the IDE port stopped with Software Failure 8000 0008, and a drive
    it did not boot from said "not mounted". FS-UAE's own loader stops at
    the last hunk, so only an emulated IDE boot - Kickstart doing the work -
    showed it.
    """

    HANDLER = Path(__file__).resolve().parent.parent / "samples" / "pfs3aio"

    def handler(self) -> bytes:
        #  A real hunk file: header, one code hunk, a reloc table, the end.
        import struct
        code = b"\x4e\x75" * 300                      # 600 bytes of RTS
        return (struct.pack(">IIIII", 0x3F3, 0, 1, 0, 0) + struct.pack(">I", 150)
                + struct.pack(">II", 0x3E9, 150) + code
                + struct.pack(">IIII", 0x3EC, 1, 0, 4) + struct.pack(">I", 0)
                + struct.pack(">I", 0x3F2))

    def test_each_block_says_how_much_it_holds(self):
        import struct
        handler = self.handler()
        table = rdb.Rdb(partitions=[rdb.Partition("DH0", 1, 9, rdb.DOSTYPE_PFS3)],
                        filesystems=[rdb.FileSystem(rdb.DOSTYPE_PFS3, handler)],
                        cylinders=10)
        image = self.scratch() / "disk.hdf"
        with open(image, "wb") as handle:
            handle.truncate(10 * 1024 * 1024)
            table.write(handle, 0)
        with open(image, "rb") as handle:
            area = handle.read(200 * 512)
        lsegs = [area[n:n + 512] for n in range(0, len(area), 512)
                 if area[n:n + 4] == b"LSEG"]
        data = b"".join(
            block[20:20 + (struct.unpack_from(">I", block, 4)[0] - 5) * 4]
            for block in lsegs)
        self.assertEqual(data[:len(handler)], handler)
        self.assertLess(len(data) - len(handler), 4,
                        "no padding after the last hunk beyond the long it ends in")
        self.assertLess(struct.unpack_from(">I", lsegs[-1], 4)[0], 128)
        with open(image, "rb") as handle:
            self.assertEqual(rdb.Rdb.read(handle, 0).filesystems[0].seglist[
                :len(handler)], handler)

    def test_padding_after_the_last_hunk_is_never_stored(self):
        handler = self.handler()
        self.assertEqual(rdb.loadable(handler + bytes(412)), handler)
        self.assertEqual(rdb.loadable(b"not a hunk file"), b"not a hunk file")

    def test_the_real_pfs3aio_is_cut_at_its_end(self):
        if not self.HANDLER.is_file():
            self.skipTest("no PFS3 handler in samples/")
        data = self.HANDLER.read_bytes()
        cut = rdb.loadable(data)
        self.assertEqual(cut[-4:], b"\x00\x00\x03\xf2", "ends at HUNK_END")
        self.assertEqual(data[:len(cut)], cut)


class TestHdfOutput(_Scratch):
    def test_creates_a_bare_amiga_drive(self):
        folder = self.scratch()
        out = folder / "made.hdf"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(out), output_hdf=True,
            emu68_prepared_dir=EMU68, image_size=64 * MIB,
            amiga_partitions=[
                builder.AmigaPartitionSpec("DH0", None, "FFS-INTL", True, 0)]),
            QUIET)
        #  A bare drive has no partition table of its own...
        with open(out, "rb") as handle:
            with self.assertRaises(ValueError):
                mbr.read_table(handle)
            #  ...but the RDB is right at block 0.
            table = rdb.Rdb.read(handle, 0)
        self.assertEqual(table.partitions[0].drive_name, "DH0")
        self.assertTrue(table.partitions[0].bootable)

    def test_hdf_output_is_refused_for_a_device(self):
        config = builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target="/dev/sdz",
            target_is_device=True, output_hdf=True)
        self.assertTrue(any("written to a file" in p for p in config.validate()))


class TestCardImageAsSource(_Scratch):
    def test_takes_only_the_amiga_drive_out_of_a_card_image(self):
        """A PiMiga-style card image: import its drive onto a different card."""
        folder = self.scratch()
        donor = folder / "donor-card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(donor),
            image_size=300 * MIB, boot_size=96 * MIB, emu68_prepared_dir=EMU68,
            amiga_partitions=[builder.AmigaPartitionSpec("DH0", None,
                                                         "FFS-INTL", True, 0)]),
            QUIET)
        info = builder.inspect_hdf(donor)
        self.assertTrue(info.from_card_image)
        self.assertGreater(info.source_offset, 0)
        self.assertLess(info.source_length, info.size,
                        "must copy the drive, not the whole card")

        rebuilt = folder / "rebuilt.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.HDF, target=str(rebuilt),
            image_size=600 * MIB, boot_size=128 * MIB, emu68_prepared_dir=EMU68,
            hdf_image=str(donor)), QUIET)

        with open(rebuilt, "rb") as handle:
            parts = mbr.read_table(handle)
            amiga = next(p for p in parts if p.type_id == mbr.TYPE_AMIGA)
            boot = next(p for p in parts if p.type_id == mbr.TYPE_FAT32_LBA)
            table = rdb.Rdb.read(handle, amiga.start_bytes)
        #  The new card has its own, larger boot partition and the donor's drive.
        self.assertEqual(boot.size_bytes, 128 * MIB)
        self.assertEqual(table.partitions[0].drive_name, "DH0")

    def test_broken_image_is_repaired_on_import(self):
        folder = self.scratch()
        broken = folder / "broken.hdf"
        make_hdf(broken, 100 * MIB, [
            rdb.Partition("DH0", 1, 50, rdb.DOSTYPE_FFS_INTL, bootable=False,
                          max_transfer=0xFFFFFF, mask=0xFFFFFFFF),
            rdb.Partition("DH0", 51, 99, rdb.DOSTYPE_FFS_INTL, bootable=False)])

        card = folder / "card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.HDF, target=str(card),
            image_size=400 * MIB, boot_size=96 * MIB, emu68_prepared_dir=EMU68,
            hdf_image=str(broken), repair_rdb=True), QUIET)

        with open(card, "rb") as handle:
            amiga = next(p for p in mbr.read_table(handle)
                         if p.type_id == mbr.TYPE_AMIGA)
            table = rdb.Rdb.read(handle, amiga.start_bytes)
        self.assertEqual(table.partitions[0].max_transfer,
                         hdfcheck.SAFE_MAX_TRANSFER)
        self.assertEqual(table.partitions[0].mask, hdfcheck.SAFE_MASK)
        self.assertTrue(any(p.bootable for p in table.partitions))
        self.assertNotEqual(table.partitions[0].drive_name,
                            table.partitions[1].drive_name)
        self.assertEqual(hdfcheck.analyse(table, amiga.size_bytes), [])

    def test_unrepairable_image_is_refused(self):
        folder = self.scratch()
        bad = folder / "overlap.hdf"
        make_hdf(bad, 100 * MIB, [
            rdb.Partition("DH0", 1, 80, rdb.DOSTYPE_FFS_INTL, bootable=True),
            rdb.Partition("DH1", 40, 99, rdb.DOSTYPE_FFS_INTL)])
        card = folder / "card.img"
        with self.assertRaises(RuntimeError) as caught:
            builder.run_build(builder.BuildConfig(
                mode=builder.BuildMode.HDF, target=str(card),
                image_size=400 * MIB, boot_size=96 * MIB, emu68_prepared_dir=EMU68,
                hdf_image=str(bad), repair_rdb=True), QUIET)
        self.assertIn("overlap", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DrawerIcons(_Scratch):
    """A drawer with no icon does not exist as far as Workbench is concerned."""

    def _volume(self, handle):
        from pistorm_imager.core import amigaos, amigafs      # noqa: PLC0415
        handle.truncate(8 * MIB)
        return amigaos.make_volume(handle, 0, (8 * MIB) // amigafs.BLOCK,
                                   "Workbench", amigafs.DOSTYPE_FFS_INTL)

    def read(self, image, path):
        """The file's bytes, so an icon can be compared exactly."""
        from pistorm_imager.core import amigaos               # noqa: PLC0415
        volume, _label = amigaos.open_amiga_volume(image)
        entry = volume.find(path)
        return volume.read_file(entry) if entry else b""

    @staticmethod
    def drawer_icon(marker: int) -> bytes:
        """A real drawer icon, tagged so the copy can be identified.

        Fake bytes will not do: only genuine drawer icons are accepted now,
        because handing a drawer a project icon is what produced "unable to
        open script" on a double click.
        """
        import struct                                        # noqa: PLC0415
        from pistorm_imager.core import amigainfo            # noqa: PLC0415
        raw = bytearray(amigainfo.DISKOBJECT_SIZE)
        struct.pack_into(">H", raw, 0, amigainfo.MAGIC)
        raw[amigainfo.TYPE_OFFSET] = amigainfo.WBDRAWER
        struct.pack_into(">I", raw, amigainfo.DRAWER_DATA, 0x1234)
        struct.pack_into(">I", raw, 74, marker)              # do_StackSize
        return bytes(raw)

    @staticmethod
    def marker_of(data: bytes) -> int:
        import struct                                        # noqa: PLC0415
        return struct.unpack_from(">I", data, 74)[0] if data else 0

    STORAGE, GENERIC = 0xA1, 0xB2

    def _icons(self):
        """A folder of drawer icons, as an icon set or a donor provides."""
        folder = self.scratch() / "icons"
        folder.mkdir()
        (folder / "Storage.info").write_bytes(self.drawer_icon(self.STORAGE))
        (folder / "Utilities.info").write_bytes(self.drawer_icon(self.GENERIC))
        #  A project icon under a name a drawer might have: it must never be
        #  chosen, however well the name matches.
        import struct                                        # noqa: PLC0415
        from pistorm_imager.core import amigainfo            # noqa: PLC0415
        bad = bytearray(amigainfo.DISKOBJECT_SIZE)
        struct.pack_into(">H", bad, 0, amigainfo.MAGIC)
        bad[amigainfo.TYPE_OFFSET] = 4                       # WBPROJECT
        (folder / "Install.info").write_bytes(bytes(bad))
        return folder

    def test_a_drawer_this_tool_made_is_given_an_icon(self):
        from pistorm_imager.core import amigaos               # noqa: PLC0415
        path = self.scratch() / "icons.hdf"
        with open(path, "w+b") as handle:
            volume = self._volume(handle)
            for drawer in ("Programs", "Internet", "Storage"):
                volume.makedirs(drawer)
            written = amigaos.ensure_drawer_icons(
                volume, ["Programs", "Internet", "Storage"],
                [self._icons()], QUIET)
            volume.close()
        self.assertEqual(written, 3)
        #  Storage gets its own; the other two fall back to a real drawer icon
        #  rather than being left invisible.
        self.assertEqual(self.marker_of(self.read(path, "Storage.info")),
                         self.STORAGE)
        self.assertEqual(self.marker_of(self.read(path, "Programs.info")),
                         self.GENERIC)
        self.assertEqual(self.marker_of(self.read(path, "Internet.info")),
                         self.GENERIC)

    def test_an_icon_that_is_already_there_is_left_alone(self):
        from pistorm_imager.core import amigaos               # noqa: PLC0415
        path = self.scratch() / "keep.hdf"
        with open(path, "w+b") as handle:
            volume = self._volume(handle)
            volume.makedirs("Programs")
            volume.write_file(volume.root, "Programs.info",
                              self.drawer_icon(0xDEAD))
            written = amigaos.ensure_drawer_icons(volume, ["Programs"],
                                                  [self._icons()], QUIET)
            volume.close()
        self.assertEqual(written, 0)
        self.assertEqual(self.marker_of(self.read(path, "Programs.info")),
                         0xDEAD)

    def test_the_drawers_workbench_hides_stay_hidden(self):
        """C: and LIBS: have no icons because Commodore chose that."""
        from pistorm_imager.core import amigaos               # noqa: PLC0415
        path = self.scratch() / "hidden.hdf"
        with open(path, "w+b") as handle:
            volume = self._volume(handle)
            for drawer in ("C", "Libs", "Devs", "S"):
                volume.makedirs(drawer)
            written = amigaos.ensure_drawer_icons(
                volume, ["C", "Libs", "Devs", "S"], [self._icons()], QUIET)
            volume.close()
        self.assertEqual(written, 0)

    def test_a_drawer_that_is_not_there_gets_nothing(self):
        from pistorm_imager.core import amigaos               # noqa: PLC0415
        path = self.scratch() / "absent.hdf"
        with open(path, "w+b") as handle:
            volume = self._volume(handle)
            written = amigaos.ensure_drawer_icons(volume, ["Programs"],
                                                  [self._icons()], QUIET)
            volume.close()
        self.assertEqual(written, 0)

    def test_a_drawer_inside_a_hidden_one_is_left_alone(self):
        """An icon in CLASSES: is only seen by someone showing all files."""
        from pistorm_imager.core import amigaos               # noqa: PLC0415
        path = self.scratch() / "nested.hdf"
        with open(path, "w+b") as handle:
            volume = self._volume(handle)
            volume.makedirs("Classes/Gadgets")
            volume.makedirs("Programs/iGame")
            written = amigaos.ensure_drawer_icons(
                volume, ["Classes/Gadgets", "Programs", "Programs/iGame"],
                [self._icons()], QUIET)
            volume.close()
        self.assertEqual(written, 2)
        self.assertEqual(self.read(path, "Classes/Gadgets.info"), b"")

    def test_a_drawer_never_wears_a_project_icon(self):
        """MagicWB's Install.info is its installer script's icon.

        Matching on the name alone gave it to the Storage/Install drawer, and
        double-clicking that answered "unable to open script" instead of
        opening a window.
        """
        from pistorm_imager.core import amigaos               # noqa: PLC0415
        path = self.scratch() / "project.hdf"
        with open(path, "w+b") as handle:
            volume = self._volume(handle)
            volume.makedirs("Storage/Install")
            written = amigaos.ensure_drawer_icons(
                volume, ["Storage/Install"], [self._icons()], QUIET)
            volume.close()
        self.assertEqual(written, 1)
        #  It fell back to a real drawer icon rather than the project icon
        #  that happened to share the name.
        self.assertEqual(self.marker_of(self.read(path,
                                                  "Storage/Install.info")),
                         self.GENERIC)


class DuplicateOverlayFile(_Scratch):
    """One duplicate file must not end a whole build.

    A tree copied as an overlay has always skipped what is already there; a
    single file raised instead, and a real build died at
    "colorwheel.gadget already exists" after installing Workbench and most of
    the software - because the dependency scan offered a file the floppies
    had already put in CLASSES:Gadgets.
    """

    def test_a_file_already_on_the_card_is_left_alone(self):
        from pistorm_imager.core import amigaos, amigafs      # noqa: PLC0415
        path = self.scratch() / "dupe.hdf"
        source = self.scratch() / "again"
        source.mkdir()
        (source / "thing.gadget").write_bytes(b"second")

        spec = builder.AmigaPartitionSpec(
            "DH0", None, "FFS-INTL", True, 0, volume_name="Sys",
            overlays=[(str(source / "thing.gadget"), "Classes/Gadgets")])

        with open(path, "w+b") as handle:
            handle.truncate(8 * MIB)
            volume = amigaos.make_volume(handle, 0, (8 * MIB) // amigafs.BLOCK,
                                         "Sys", amigafs.DOSTYPE_FFS_INTL)
            parent = volume.makedirs("Classes/Gadgets")
            volume.write_file(parent, "thing.gadget", b"first")
            #  This raised before, and took the whole build with it.
            builder._apply_overlays(volume, spec, None, QUIET)
            volume.close()

        volume, _label = amigaos.open_amiga_volume(path)
        kept = volume.read_file(volume.find("Classes/Gadgets/thing.gadget"))
        volume.f.close()
        self.assertEqual(kept, b"first", "the existing file was replaced")

class TestABootableDriveFilledFromAnImage(_Scratch):
    """The path that fills the boot drive from another drive: nothing
    exercised it, and a name that did not exist there ended a real build
    after the partition table had already been written."""

    def _source(self):
        """A small Amiga drive to import."""
        tree = self.scratch() / "source"
        (tree / "S").mkdir(parents=True)
        (tree / "S" / "Startup-Sequence").write_text("echo hello\n")
        (tree / "C").mkdir()
        (tree / "C" / "LoadWB").write_bytes(b"x" * 64)
        made = self.scratch() / "source.hdf"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(made), output_hdf=True,
            image_size=40 * MIB, emu68_prepared_dir=EMU68,
            pfs3_binary=PFS3_HANDLER,
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0, volume_name="Src",
                content_folder=str(tree))]), QUIET)
        return made

    def test_a_card_can_be_built_around_an_imported_drive(self):
        source = self._source()
        out = self.scratch() / "card.hdf"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(out), output_hdf=True,
            image_size=80 * MIB, emu68_prepared_dir=EMU68, fix_compatibility=True,
            pfs3_binary=PFS3_HANDLER,
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0, volume_name="Sys",
                content_hdf=str(source))]), QUIET)
        from pistorm_imager.core import amigaos
        volume, _label = amigaos.open_amiga_volume(str(out))
        self.assertIsNotNone(volume.find("C/LoadWB"),
                             "the imported drive's files are not on the card")


class EveryDriveWearsTheCardsIcon(_Scratch):
    """A drive with no Disk.info never appears on the Workbench desktop.

    Two faults, one rule. A drive this build formats and names but fills with
    nothing got no volume icon at all - so the Work drive was created, given a
    name and then invisible, which reads as the partition having failed. And a
    drive filled from somebody else's tree wore *their* volume icon: PiMiga's
    Games and Demos arrive with an 8 KB icon drawn for a different desktop,
    beside a system drive wearing its own.

    So every drive on the card wears the boot drive's icon, and the boot drive
    keeps the one it came with.
    """

    def build(self, source, extra_specs=()):
        out = self.scratch() / "card.hdf"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(out), output_hdf=True,
            image_size=120 * MIB, emu68_prepared_dir=EMU68, fix_compatibility=True,
            pfs3_binary=PFS3_HANDLER,
            amiga_partitions=[
                builder.AmigaPartitionSpec(
                    "DH0", 60 * MIB, "PFS3", True, 0, volume_name="Sys",
                    content_folder=str(source)),
                *extra_specs]), QUIET)
        return out

    def volume(self, image, name):
        """One named drive out of the card, so each can be checked apart."""
        from pistorm_imager.core import amigaos                  # noqa: PLC0415
        volume, _label = amigaos.open_amiga_volume(str(image), name)
        self.addCleanup(lambda: volume.f.close())
        return volume

    def source(self):
        folder = self.scratch() / "sys"
        (folder / "S").mkdir(parents=True)
        (folder / "S" / "Startup-Sequence").write_bytes(b"C:LoadWB\n")
        (folder / "Disk.info").write_bytes(b"the card's own volume icon")
        return folder

    def test_a_drive_filled_with_nothing_still_gets_an_icon(self):
        image = self.build(self.source(), [builder.AmigaPartitionSpec(
            "DH1", None, "PFS3", False, -128, volume_name="Work")])
        volume = self.volume(image, "DH1")
        entry = volume.find("Disk.info")
        self.assertIsNotNone(
            entry, "Work has no Disk.info, so it never appears on Workbench")
        self.assertEqual(volume.read_file(entry)[:9], b"the card'")

    def test_a_filled_drive_does_not_keep_the_donors_icon(self):
        other = self.scratch() / "games"
        other.mkdir()
        (other / "Disk.info").write_bytes(b"somebody else's icon entirely")
        (other / "AGame").write_bytes(b"x")
        image = self.build(self.source(), [builder.AmigaPartitionSpec(
            "DH1", None, "PFS3", False, -128, volume_name="Games",
            content_folder=str(other))])
        volume = self.volume(image, "DH1")
        entry = volume.find("Disk.info")
        self.assertIsNotNone(entry)
        self.assertEqual(volume.read_file(entry)[:9], b"the card'",
                         "the drive kept the icon its source came with")
        #  The content itself is untouched.
        self.assertIsNotNone(volume.find("AGame"))

    def test_the_boot_drive_keeps_its_own(self):
        image = self.build(self.source())
        volume = self.volume(image, "DH0")
        entry = volume.find("Disk.info")
        self.assertIsNotNone(entry)
        self.assertEqual(volume.read_file(entry), b"the card's own volume icon")


class ExportingDrivesAsSeparateFiles(_Scratch):
    """Reading the Amiga drives back out of a card, one file each.

    The old answer - write the *output* as a bare .hdf - could only ever
    describe one drive, and a PiStorm card normally carries four.
    """

    def _card(self, drives):
        """A small card image with an RDB and the named drives in it."""
        from pistorm_imager.core import rdb                     # noqa: PLC0415
        folder = self.scratch()
        path = folder / "card.img"
        geom = rdb.Geometry()
        cyl = geom.cyl_blocks * geom.block_size
        total = (len(drives) + 3) * 4 * cyl
        parts = rdb.layout(geom, total // geom.block_size,
                           [(n, 2 * cyl, rdb.parse_dostype(d)) for n, d in drives])
        parts[0].bootable = True
        table = rdb.Rdb(geometry=geom, partitions=parts,
                        filesystems=[rdb.FileSystem(
                            dostype=rdb.parse_dostype("PFS3"),
                            seglist=b"handler-bytes" * 64, version=19 << 16 | 2)],
                        cylinders=total // cyl)
        with open(path, "wb") as handle:
            handle.truncate(total)
            table.write(handle, 0)
            #  Something recognisable in each drive, to prove the right
            #  blocks travel into the right file.
            for index, part in enumerate(table.partitions):
                handle.seek(part.low_cyl * cyl)
                handle.write(bytes([index + 1]) * 4096)
        return path, table

    def test_every_drive_is_found_with_its_type_and_size(self):
        from pistorm_imager.core import export                   # noqa: PLC0415
        path, _ = self._card([("DH0", "PFS3"), ("DH1", "PFS3"), ("DH2", "FFS-INTL")])
        found = export.drives(path)
        self.assertEqual([d.name for d in found], ["DH0", "DH1", "DH2"])
        self.assertTrue(found[0].bootable)
        self.assertFalse(found[1].bootable)
        self.assertEqual(found[2].dostype, rdb_dostype("FFS-INTL"))

    def test_each_chosen_drive_becomes_a_self_contained_file(self):
        from pistorm_imager.core import builder, export          # noqa: PLC0415
        path, _ = self._card([("DH0", "PFS3"), ("DH1", "PFS3")])
        out = self.scratch()
        written = export.export(path, ["DH1"], out, QUIET)
        self.assertEqual(len(written), 1)
        with open(written[0], "rb") as handle:
            located = builder.find_rdb(handle)
            self.assertIsNotNone(located, "the exported file has its own RDB")
            _base, table = located
            #  One drive, named as it was, and the handler travelled with it -
            #  without which an emulator cannot read a PFS3 drive at all.
            self.assertEqual([p.drive_name for p in table.partitions], ["DH1"])
            self.assertTrue(table.filesystems, "the PFS3 handler is embedded")
            self.assertEqual(table.filesystems[0].dostype,
                             rdb_dostype("PFS3"))

    def test_the_right_blocks_land_in_the_right_file(self):
        """The check that a name-only test would pass while shipping DH0."""
        from pistorm_imager.core import builder, export          # noqa: PLC0415
        path, _ = self._card([("DH0", "PFS3"), ("DH1", "PFS3"), ("DH2", "PFS3")])
        out = self.scratch()
        written = export.export(path, ["DH2"], out, QUIET)
        with open(written[0], "rb") as handle:
            _base, table = builder.find_rdb(handle)
            part = table.partitions[0]
            handle.seek(part.low_cyl * table.geometry.cyl_blocks
                        * table.geometry.block_size)
            #  DH2 is the third drive, so its blocks are filled with 3.
            self.assertEqual(handle.read(16), bytes([3]) * 16)

    def test_a_drive_that_is_not_there_is_refused_by_name(self):
        from pistorm_imager.core import export                   # noqa: PLC0415
        path, _ = self._card([("DH0", "PFS3")])
        with self.assertRaises(RuntimeError) as caught:
            export.export(path, ["DH7"], self.scratch(), QUIET)
        self.assertIn("DH7", str(caught.exception))
        self.assertIn("DH0", str(caught.exception), "and says what it does hold")

    def test_an_image_with_no_rdb_yields_no_drives(self):
        from pistorm_imager.core import export                   # noqa: PLC0415
        blank = self.scratch() / "blank.img"
        blank.write_bytes(b"\0" * (1 << 20))
        self.assertEqual(export.drives(blank), [])


def rdb_dostype(name):
    from pistorm_imager.core import rdb                          # noqa: PLC0415
    return rdb.parse_dostype(name)
