"""Tests for hard disk image import/export and the PiStorm compatibility repair."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import builder, hdfcheck, mbr, rdb  # noqa: E402
from pistorm_imager.core.util import GIB, MIB, Progress  # noqa: E402

QUIET = Progress()


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
            image_size=300 * MIB, boot_size=96 * MIB, install_emu68=False,
            amiga_partitions=[builder.AmigaPartitionSpec("DH0", None,
                                                         "FFS-INTL", True, 0)]),
            QUIET)
        with open(card, "rb") as handle:
            found = builder.find_rdb(handle)
        self.assertIsNotNone(found)
        self.assertGreater(found[0], 0, "should be at the 0x76 partition, not block 0")
        self.assertEqual(found[1].partitions[0].drive_name, "DH0")


class TestHdfOutput(_Scratch):
    def test_creates_a_bare_amiga_drive(self):
        folder = self.scratch()
        out = folder / "made.hdf"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(out), output_hdf=True,
            install_emu68=False, image_size=64 * MIB,
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
            image_size=300 * MIB, boot_size=96 * MIB, install_emu68=False,
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
            image_size=600 * MIB, boot_size=128 * MIB, install_emu68=False,
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
            image_size=400 * MIB, boot_size=96 * MIB, install_emu68=False,
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
                image_size=400 * MIB, boot_size=96 * MIB, install_emu68=False,
                hdf_image=str(bad), repair_rdb=True), QUIET)
        self.assertIn("overlap", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
