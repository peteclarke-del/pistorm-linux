"""Tests for the Amiga file system layer and the ADF installer.

Where the sample disks shipped in ``samples/`` are present these run against
real Workbench 3.1 images; otherwise those tests skip.
"""
import os
import struct
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pistorm_imager.core import amigafs, amigaos, pfs3  # noqa: E402
from pistorm_imager.core.amigafs import Volume, VolumeWriter  # noqa: E402
from pistorm_imager.core.util import MIB, Progress  # noqa: E402

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

SAMPLE_ADFS = ROOT / "samples" / "workbench"
HAVE_SAMPLES = SAMPLE_ADFS.is_dir() and any(SAMPLE_ADFS.glob("*.adf"))


def new_volume(folder: Path, blocks: int, name: str = "Test",
               dostype: int = amigafs.DOSTYPE_FFS_INTL):
    path = folder / "vol.img"
    handle = open(path, "w+b")
    handle.truncate(blocks * amigafs.BLOCK)
    volume = VolumeWriter.format(handle, 0, blocks, name, dostype=dostype)
    return volume, handle, path


class TestHashing(_Scratch):
    def test_hash_is_case_insensitive(self):
        self.assertEqual(amigafs.hash_name("Startup-Sequence", True),
                         amigafs.hash_name("startup-sequence", True))

    def test_hash_stays_in_range(self):
        for name in ("A", "workbench.library", "x" * 30, "Zzz", "System"):
            self.assertTrue(0 <= amigafs.hash_name(name, True) < amigafs.HT_SIZE)

    def test_international_mode_folds_accents(self):
        #  In intl mode 0xE0-0xFE (except 0xF7) upper-case like a-z do.
        self.assertEqual(amigafs.hash_name("\xe9", True), amigafs.hash_name("\xc9", True))
        self.assertNotEqual(amigafs.hash_name("\xe9", False),
                            amigafs.hash_name("\xc9", False))


class TestVolumeWriting(_Scratch):
    def test_round_trip_of_a_tree(self):
        volume, handle, path = new_volume(self.scratch(), 20000)
        libs = volume.makedirs("Libs")
        payload = os.urandom(250_000)          # spans several extension blocks
        volume.write_file(libs, "big.library", payload)
        volume.write_file(volume.root, "Startup-Sequence", b"echo hi\n")
        volume.makedirs("S/Deep/Nested")
        volume.close()

        reader_handle = open(path, "rb")
        reader = Volume(reader_handle)
        self.addCleanup(reader_handle.close)
        self.assertEqual(reader.name, "Test")
        self.assertTrue(reader.ffs)
        self.assertEqual(reader.read_file(reader.find("Libs/big.library")), payload)
        self.assertEqual(reader.read_file(reader.find("Startup-Sequence")), b"echo hi\n")
        self.assertIsNotNone(reader.find("S/Deep/Nested"))
        handle.close()

    def test_empty_file(self):
        volume, handle, path = new_volume(self.scratch(), 4000)
        volume.write_file(volume.root, "empty", b"")
        volume.close()
        reader_handle = open(path, "rb")
        reader = Volume(reader_handle)
        self.addCleanup(reader_handle.close)
        self.assertEqual(reader.read_file(reader.find("empty")), b"")
        handle.close()

    def test_many_files_exercise_hash_chains(self):
        volume, handle, path = new_volume(self.scratch(), 20000)
        names = [f"file{i:03}" for i in range(200)]   # far more than 72 hash slots
        for name in names:
            volume.write_file(volume.root, name, name.encode())
        volume.close()
        reader_handle = open(path, "rb")
        reader = Volume(reader_handle)
        self.addCleanup(reader_handle.close)
        found = {e.name for e in reader.listdir()}
        self.assertEqual(found, set(names))
        for name in names:
            self.assertEqual(reader.read_file(reader.find(name)), name.encode())
        handle.close()

    def test_all_block_checksums_are_valid(self):
        volume, handle, path = new_volume(self.scratch(), 8000)
        volume.write_file(volume.makedirs("C"), "List", os.urandom(90_000))
        volume.close()
        reader_handle = open(path, "rb")
        reader = Volume(reader_handle)
        self.addCleanup(reader_handle.close)
        #  Root, directory and file header blocks all checksum at offset 20.
        checked = 0
        for _p, entry in reader.walk():
            block = reader.read_block(entry.block)
            self.assertTrue(amigafs.verify_checksum(block),
                            f"bad checksum on {entry.name}")
            checked += 1
        self.assertTrue(amigafs.verify_checksum(reader.read_block(reader.root)))
        self.assertGreater(checked, 0)
        handle.close()

    def test_bitmap_never_marks_a_used_block_free(self):
        """The audit that matters: corruption is a used block marked free."""
        volume, handle, path = new_volume(self.scratch(), 20000)
        volume.write_file(volume.makedirs("Libs"), "a.library", os.urandom(200_000))
        volume.write_file(volume.root, "b", os.urandom(5_000))
        volume.close()

        reader_handle = open(path, "rb")
        reader = Volume(reader_handle)
        self.addCleanup(reader_handle.close)
        root = reader.read_block(reader.root)
        pointers = [struct.unpack_from(">I", root, 316 + i * 4)[0]
                    for i in range(amigafs.ROOT_BM_PAGES)]
        pointers = [p for p in pointers if p]
        ext = struct.unpack_from(">I", root, 416)[0]
        while ext:
            block = reader.read_block(ext)
            pointers += [x for x in
                         (struct.unpack_from(">I", block, i * 4)[0]
                          for i in range(amigafs.BITMAP_LONGS)) if x]
            ext = struct.unpack_from(">I", block, amigafs.BLOCK - 4)[0]

        free = set()
        for index, number in enumerate(pointers):
            block = reader.read_block(number)
            for long_index in range(amigafs.BITMAP_LONGS):
                value = struct.unpack_from(">I", block, 4 + long_index * 4)[0]
                for bit in range(32):
                    if value >> bit & 1:
                        block_id = (reader.reserved
                                    + index * amigafs.BITS_PER_BITMAP
                                    + long_index * 32 + bit)
                        if block_id < reader.total_blocks:
                            free.add(block_id)

        used = set(range(reader.reserved)) | {reader.root} | set(pointers)
        for _p, entry in reader.walk():
            used.add(entry.block)
            if entry.is_file:
                current = entry.block
                while current:
                    block = reader.read_block(current)
                    high_seq = struct.unpack_from(">I", block, 8)[0]
                    data = struct.unpack_from(f">{amigafs.HT_SIZE}I", block, 24)
                    for i in range(high_seq):
                        pointer = data[amigafs.HT_SIZE - 1 - i]
                        if pointer:
                            used.add(pointer)
                    current = struct.unpack_from(">I", block, 504)[0]
                    if current:
                        used.add(current)
        self.assertEqual(used & free, set(),
                         "blocks that are in use are marked free in the bitmap")
        handle.close()

    def test_ofs_volumes_are_refused_for_writing(self):
        path = self.scratch() / "ofs.img"
        handle = open(path, "w+b")
        handle.truncate(1000 * amigafs.BLOCK)
        with self.assertRaises(amigafs.AmigaFsError):
            VolumeWriter.format(handle, 0, 1000, "Old", dostype=amigafs.DOSTYPE_OFS)
        handle.close()


@unittest.skipUnless(HAVE_SAMPLES, "no sample ADFs in samples/workbench")
class TestRealWorkbenchDisks(_Scratch):
    def test_reads_a_real_adf(self):
        #  Pick the disk by what is inside it: every file in the set is named
        #  "Workbench v3.1 ...", so the file name says nothing useful.
        disks = amigaos.scan(SAMPLE_ADFS)
        workbench = next(m for m in disks if m.role and m.role.key == "workbench")
        with open(workbench.path, "rb") as handle:
            volume = Volume(handle)
            self.assertEqual(volume.name, "Workbench3.1")
            self.assertTrue(volume.ffs)
            self.assertEqual(volume.total_blocks, 1760)
            entry = volume.find("Libs/asl.library")
            self.assertIsNotNone(entry)
            self.assertEqual(len(volume.read_file(entry)), entry.size)

    def test_identifies_the_full_install_set(self):
        disks = amigaos.scan(SAMPLE_ADFS)
        chosen = amigaos.choose_set(disks)
        self.assertEqual(amigaos.missing_roles(chosen), [])
        self.assertEqual({m.role.key for m in chosen.values()},
                         {"workbench", "extras", "fonts", "locale", "storage", "install"})
        for match in chosen.values():
            self.assertEqual(match.version, "3.1",
                             f"{match.path.name} is not from the 3.1 set")

    def test_installs_workbench_onto_a_volume(self):
        disks = amigaos.scan(SAMPLE_ADFS)
        chosen = amigaos.choose_set(disks, "3.1")
        blocks = 60 * MIB // amigafs.BLOCK
        path = self.scratch() / "dh0.hdf"
        handle = open(path, "w+b")
        handle.truncate(blocks * amigafs.BLOCK)
        amigaos.install(handle, 0, blocks, chosen, QUIET, volume_name="Workbench")
        handle.flush()

        reader_handle = open(path, "rb")
        reader = Volume(reader_handle)
        self.addCleanup(reader_handle.close)
        self.assertEqual(reader.name, "Workbench")
        names = {e.name for e in reader.listdir() if e.is_dir}
        for expected in ("C", "Libs", "Devs", "S", "System", "Fonts", "Locale",
                         "Storage", "Utilities", "WBStartup", "Prefs", "Tools"):
            self.assertIn(expected, names)
        #  The startup sequence is what makes the partition boot at all.
        startup = reader.find("S/Startup-Sequence")
        self.assertIsNotNone(startup)
        self.assertGreater(startup.size, 0)
        #  Files must survive the copy byte for byte, with their metadata.
        source_handle = open(chosen["workbench"].path, "rb")
        source = Volume(source_handle)
        self.addCleanup(source_handle.close)
        original = source.find("Libs/asl.library")
        copied = reader.find("Libs/asl.library")
        self.assertEqual(reader.read_file(copied), source.read_file(original))
        self.assertEqual(copied.protect, original.protect)
        self.assertEqual((copied.days, copied.mins, copied.ticks),
                         (original.days, original.mins, original.ticks))
        handle.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestNamePlanning(unittest.TestCase):
    """Renaming a file breaks whatever refers to it by name.

    A WHDLoad slave and an icon's tool types both name files, so shortening one
    silently stops a game working.  Where a name must be shortened it has to be
    done consistently, and where it need not be it must be left alone.
    """

    LONG = [
        "backslide-cosmicorbs-hdd.exe", "backslide-cosmicorbs-hdd.exe.info",
        "1%25AiDS 68060 experimental.exe",
        "1%25AiDS 68060 experimental.exe.info",
        "test_engine_perlintunel2.vars.info",
        "BOOM!PARTY_2025_Invitation4.nfo",
        "BOOM!PARTY_2025_Invitation4.nfo.info",
    ]

    def test_icons_keep_the_name_of_their_file(self):
        plan = amigaos.plan_names(self.LONG, limit=30)
        for original in self.LONG:
            if not original.endswith(".info"):
                continue
            base = original[:-len(".info")]
            if base in plan:
                self.assertEqual(plan[original], plan[base] + ".info",
                                 f"{original} was orphaned from its file")

    def test_nothing_exceeds_the_limit(self):
        for limit in (30, 40, 106):
            plan = amigaos.plan_names(self.LONG, limit=limit)
            for original, chosen in plan.items():
                self.assertLessEqual(len(chosen), limit, f"{original} -> {chosen}")

    def test_no_two_entries_collide(self):
        plan = amigaos.plan_names(self.LONG, limit=30)
        lowered = [n.lower() for n in plan.values()]
        self.assertEqual(len(set(lowered)), len(lowered),
                         "two entries shortened onto the same name")

    def test_truncation_never_leaves_a_double_dot(self):
        plan = amigaos.plan_names(self.LONG, limit=30)
        for chosen in plan.values():
            self.assertNotIn("..", chosen)

    def test_names_that_fit_are_left_alone(self):
        plan = amigaos.plan_names(self.LONG, limit=106)
        self.assertEqual(plan, {n: n for n in self.LONG},
                         "a name that fits must not be touched")

    def test_icons_match_their_file_whatever_the_case(self):
        """AmigaDOS is case-insensitive, and real collections rely on it.

        A tree copied off a case-sensitive host is full of icons spelt
        differently from their file. Comparing case-sensitively invents a clash
        and renames a file that was perfectly good.
        """
        entries = ["Eagleplayer", "EaglePlayer.info",
                   "Eagleplayer.readme", "Eagleplayer.ReadMe.info",
                   "Sounddrivers", "SoundDrivers.info",
                   "milkytracker.68k", "MilkyTracker.68k.info"]
        plan = amigaos.plan_names(entries, limit=106)
        self.assertEqual(plan, {n: n for n in entries},
                         "nothing here needed renaming")

    def test_trailing_dots_are_kept(self):
        """Only ":" and "/" are reserved on AmigaDOS; a trailing dot is fine."""
        entries = ["MOD.doober.", "MOD.e.c.t.", "MOD.fast speed inc.", "..."]
        for limit in (30, 106):
            self.assertEqual(amigaos.plan_names(entries, limit=limit),
                             {n: n for n in entries}, f"at limit {limit}")

    def test_truncation_does_not_end_on_a_dot(self):
        long_name = "MOD." + "a very long module name that will not fit" + "."
        chosen = amigaos.plan_names([long_name], limit=30)[long_name]
        self.assertLessEqual(len(chosen), 30)
        self.assertFalse(chosen.endswith("."), chosen)

    def test_a_file_called_dot_info_is_left_alone(self):
        """That is an ordinary name, not an icon belonging to nothing."""
        plan = amigaos.plan_names([".info", "Disk.info", "Disk"], limit=106)
        self.assertEqual(plan[".info"], ".info")

    def test_a_genuine_case_clash_is_still_separated(self):
        """Two files differing only in case cannot both exist on the Amiga."""
        plan = amigaos.plan_names(["Readme", "README"], limit=106)
        self.assertNotEqual(plan["Readme"].lower(), plan["README"].lower())

    def test_icons_stay_paired_when_a_name_must_be_shortened(self):
        entries = ["Eagleplayer-with-a-very-long-name-indeed.exe",
                   "EAGLEPLAYER-with-a-very-long-name-indeed.exe.info"]
        plan = amigaos.plan_names(entries, limit=30)
        self.assertEqual(plan[entries[1]].lower(),
                         plan[entries[0]].lower() + ".info")
        self.assertTrue(all(len(v) <= 30 for v in plan.values()))

    def test_a_generous_limit_avoids_renaming_entirely(self):
        short = ["Disk.info", "C", "S", "Startup-Sequence"]
        self.assertEqual(amigaos.plan_names(short, limit=30),
                         {n: n for n in short})


class TestNameLimits(_Scratch):
    def test_ffs_reports_thirty(self):
        volume, handle, _path = new_volume(self.scratch(), 8000)
        self.assertEqual(amigaos.name_limit(volume), 30)
        volume.close()
        handle.close()

    def test_pfs3_reports_its_own_and_keeps_long_names(self):
        folder = self.scratch()
        path = folder / "vol.hdf"
        blocks = 60000
        handle = open(path, "w+b")
        self.addCleanup(handle.close)
        handle.truncate(blocks * amigafs.BLOCK)
        volume = pfs3.Pfs3Writer(handle, 0, blocks, "Games")
        volume.format()
        self.assertGreater(amigaos.name_limit(volume), 30)

        long_name = "1%25AiDS 68060 experimental.exe.info"
        volume.write_file(volume.root, long_name, b"x" * 64)
        volume.close()
        handle.flush()

        reader = pfs3.Pfs3Volume(open(path, "rb"))
        self.addCleanup(reader.f.close)
        self.assertGreater(reader.fnsize, 32, "the volume records its own limit")
        self.assertIn(long_name, [e.name for e in reader.listdir()])
