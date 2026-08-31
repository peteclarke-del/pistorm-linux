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

from pistorm_imager.core import amigafs, amigainfo, amigaos, pfs3  # noqa: E402
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


def make_icon(tooltypes: list[str]) -> bytes:
    """A minimal but structurally valid .info file carrying tool types.

    The same helper as in ``test_compat``; kept here so each test file stands
    on its own.
    """
    data = bytearray(78)
    struct.pack_into(">HH", data, 0, amigainfo.MAGIC, 1)
    struct.pack_into(">I", data, 54, 1)          # do_ToolTypes present
    block = bytearray(struct.pack(">I", (len(tooltypes) + 1) * 4))
    for entry in tooltypes:
        raw = entry.encode("latin-1") + b"\0"
        block += struct.pack(">I", len(raw)) + raw
    return bytes(data + block)


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

    def test_a_preferred_spelling_is_the_one_that_keeps_its_name(self):
        """The caller decides which of two indistinguishable names matters."""
        entries = ["Driller.Slave", "Driller.slave"]
        plan = amigaos.plan_names(entries, limit=106,
                                  preferred=frozenset({"Driller.slave"}))
        self.assertEqual(plan["Driller.slave"], "Driller.slave")
        self.assertNotEqual(plan["Driller.Slave"].lower(), "driller.slave")
        #  Without a preference the order is settled, but by the names alone.
        plain = amigaos.plan_names(entries, limit=106)
        self.assertEqual(plain["Driller.Slave"], "Driller.Slave")

    def test_a_generous_limit_avoids_renaming_entirely(self):
        short = ["Disk.info", "C", "S", "Startup-Sequence"]
        self.assertEqual(amigaos.plan_names(short, limit=30),
                         {n: n for n in short})


class TestHostNamesInAmigaSpelling(unittest.TestCase):
    """Amiga names are ISO-8859-1 bytes, and Linux keeps file names as bytes.

    A tree lifted off an Amiga therefore arrives with names Python cannot
    decode as UTF-8. Rewriting the byte it could not read replaces a name that
    was already perfectly good: "portugu?s.language" is a locale AmigaOS will
    never find, and "?" is a pattern wildcard to AmigaDOS at that.
    """

    def test_a_name_stored_as_amiga_bytes_is_kept_exactly(self):
        raw = os.fsdecode(b"portugu\xeas.language")
        chosen = amigaos.plan_names([raw], limit=106)[raw]
        self.assertEqual(chosen, "portugu\u00eas.language")
        self.assertEqual(chosen.encode("latin-1"), b"portugu\xeas.language")

    def test_a_utf8_name_iso_8859_1_can_hold_keeps_its_letters(self):
        name = "t\u00fcrk\u00e7e"
        self.assertEqual(amigaos.plan_names([name], limit=106)[name], name)

    def test_a_letter_iso_8859_1_lacks_is_folded_rather_than_lost(self):
        name = "\u010de\u0161tina"          # Czech, which ISO-8859-1 has no room for
        self.assertEqual(amigaos.plan_names([name], limit=106)[name], "cestina")

    def test_the_two_spellings_of_one_name_are_seen_as_one(self):
        """PiMiga's Locale drawer holds both, and the Amiga has only one name."""
        latin1 = os.fsdecode(b"espa\xf1a.country")
        utf8 = "espa\u00f1a.country"
        plan = amigaos.plan_names([latin1, utf8], limit=106)
        self.assertNotEqual(plan[latin1].lower(), plan[utf8].lower())


class TestInstallingAHostTree(_Scratch):
    """Copying a Linux directory tree - PiMiga's drives - onto a volume.

    Such a tree was assembled where case matters and the Amiga's does not, so
    it is full of pairs the Amiga cannot keep apart: "Bombuzal.slave" beside an
    identical "Bombuzal.Slave", "data" beside "Data". Renaming one of each pair
    wastes space, reports a change nothing asked for, and - for a drawer -
    leaves a game looking for half of its files.
    """

    def install(self, build, blocks: int = 20000, pfs3_volume: bool = False):
        source = self.scratch()
        build(source)
        if pfs3_volume:
            path = self.scratch() / "vol.hdf"
            handle = open(path, "w+b")
            handle.truncate(blocks * amigafs.BLOCK)
            volume = pfs3.Pfs3Writer(handle, 0, blocks, "Test")
            volume.format()
        else:
            volume, handle, path = new_volume(self.scratch(), blocks)
        log: list[str] = []
        copied, renamed = amigaos.install_tree(
            volume, source, "", Progress(on_log=log.append))
        volume.close()
        handle.close()
        back = open(path, "rb")
        self.addCleanup(back.close)
        self.reader = (pfs3.Pfs3Volume(back, 0) if pfs3_volume else Volume(back))
        listing = {p: e for p, e in self.reader.walk()}
        return listing, copied, renamed, log

    def test_an_identical_copy_differing_only_in_case_is_left_out(self):
        def build(source: Path):
            (source / "Bombuzal.slave").write_bytes(b"slave")
            (source / "Bombuzal.Slave").write_bytes(b"slave")
        listing, copied, renamed, log = self.install(build)
        self.assertEqual(len(listing), 1, listing)
        self.assertEqual(copied, 1)
        self.assertEqual(renamed, 0, "nothing here needed a new name")
        self.assertNotIn("_2", "".join(listing))
        self.assertTrue(any("left out" in line for line in log), log)

    def test_the_copy_nothing_could_open_is_left_out_too(self):
        """Two files of the same name, differing - and only one is reachable.

        Every spelling of a name finds the same entry on an Amiga volume, so a
        second copy kept as "Driller_2.slave" is a file nothing would ever ask
        for.  Leaving it out is what the drawer looked like to the Amiga all
        along; keeping it only fills the card with names that were never there.
        """
        def build(source: Path):
            (source / "Driller.slave").write_bytes(b"one version")
            (source / "Driller.Slave").write_bytes(b"a different version")
        listing, copied, renamed, log = self.install(build)
        self.assertEqual(len(listing), 1, listing)
        self.assertEqual(copied, 1)
        self.assertEqual(renamed, 0, "nothing was renamed; one was left out")
        self.assertNotIn("_2", "".join(listing))
        self.assertTrue([line for line in log if "left out" in line], log)

    def test_two_drawers_of_the_same_name_become_one(self):
        def build(source: Path):
            (source / "data").mkdir()
            (source / "data" / "one.dat").write_bytes(b"1")
            (source / "Data").mkdir()
            (source / "Data" / "two.dat").write_bytes(b"2")
        listing, copied, renamed, _log = self.install(build)
        drawers = [p for p, e in listing.items() if e.is_dir]
        self.assertEqual(len(drawers), 1, listing)
        drawer = drawers[0]
        self.assertEqual(sorted(listing) ,
                         sorted([drawer, f"{drawer}/one.dat", f"{drawer}/two.dat"]))
        self.assertEqual(copied, 2)
        self.assertEqual(renamed, 0)

    def test_the_slave_an_icon_names_is_the_one_that_keeps_its_name(self):
        """Which of two spellings gives way is not arbitrary.

        A collection built for an emulator that mounts the host directory keeps
        both builds of a slave, and the emulator opens the exact spelling the
        icon names.  Keeping the other one would start a different build of the
        game here than the same collection runs there.
        """
        def build(source: Path):
            (source / "Driller.Slave").write_bytes(b"an older build")
            (source / "Driller.slave").write_bytes(b"the build the icon names")
            (source / "Driller.info").write_bytes(
                make_icon(["WHDLoad", "SLAVE=Driller.slave", "PRELOAD"]))
        listing, _copied, _renamed, _log = self.install(build)
        self.assertEqual(sorted(listing), ["Driller.info", "Driller.slave"],
                         f"the wrong build survived ({sorted(listing)})")
        self.assertEqual(self.reader.read_file(listing["Driller.slave"]),
                         b"the build the icon names")

    def test_without_an_icon_to_go_on_the_choice_is_still_settled(self):
        """Nothing names either spelling, so it only has to be repeatable."""
        def build(source: Path):
            (source / "Driller.Slave").write_bytes(b"an older build")
            (source / "Driller.slave").write_bytes(b"another build")
        first, _copied, renamed, _log = self.install(build)
        second, _copied, _renamed, _log = self.install(build)
        self.assertEqual(renamed, 0)
        self.assertEqual(len(first), 1, first)
        self.assertEqual(sorted(first), sorted(second),
                         "the same tree must plan the same way twice")

    def test_an_icon_is_repointed_at_the_file_it_names(self):
        """Shortening a file and leaving its icon alone breaks the icon.

        An icon's tool types are how a WHDLoad game names its slave, which is
        exactly what the length warning is about; where the new name is known,
        the reference can simply be corrected.
        """
        long_name = "Ambermoon-with-a-name-far-too-long-for-ffs.slave"
        def build(source: Path):
            (source / long_name).write_bytes(b"the slave")
            (source / "Ambermoon.info").write_bytes(
                make_icon(["WHDLoad", f"SLAVE={long_name}", "PRELOAD"]))
        listing, _copied, _renamed, log = self.install(build)
        shortened = next(n for n in listing if n.lower().endswith(".slave"))
        self.assertLessEqual(len(shortened), 30)
        icon = self.reader.read_file(listing["Ambermoon.info"])
        self.assertEqual(amigainfo.read_tooltypes(icon),
                         ["WHDLoad", f"SLAVE={shortened}", "PRELOAD"])
        self.assertTrue([line for line in log if "had to be renamed" in line], log)

    def test_a_reference_spelt_in_another_case_is_repointed_too(self):
        """AmigaDOS matches without regard to case, so the icon may not match."""
        long_name = "Ambermoon-with-a-name-far-too-long-for-ffs.slave"
        def build(source: Path):
            (source / long_name).write_bytes(b"the slave")
            (source / "Ambermoon.info").write_bytes(
                make_icon([f"(SLAVE={long_name.upper()})"]))
        listing, _copied, _renamed, _log = self.install(build)
        shortened = next(n for n in listing if n.lower().endswith(".slave"))
        icon = self.reader.read_file(listing["Ambermoon.info"])
        self.assertEqual(amigainfo.read_tooltypes(icon),
                         [f"(SLAVE={shortened})"],
                         "a disabled tool type must keep its brackets")

    def test_an_icon_naming_nothing_that_moved_is_left_exactly_as_it_was(self):
        original = make_icon(["BOARDTYPE=VideoCore", "SLAVE=Elsewhere.slave"])
        def build(source: Path):
            (source / "Ambermoon.slave").write_bytes(b"the slave")
            (source / "Ambermoon.info").write_bytes(original)
        listing, _copied, renamed, _log = self.install(build)
        self.assertEqual(renamed, 0)
        self.assertEqual(self.reader.read_file(listing["Ambermoon.info"]),
                         original, "an icon with nothing to fix must not be touched")

    def test_the_log_only_warns_about_length_when_a_name_was_shortened(self):
        def build(source: Path):
            (source / "Readme").write_bytes(b"a")
            (source / "ReadMe").write_bytes(b"a")
        _listing, _copied, _renamed, log = self.install(build)
        self.assertFalse([line for line in log if "shortened to fit" in line],
                         "nothing was shortened, so nothing may say it was")

    def test_a_name_too_long_for_the_volume_still_says_so(self):
        def build(source: Path):
            (source / ("a-very-long-name-that-ffs-cannot-hold" * 2)).write_bytes(b"a")
        _listing, _copied, renamed, log = self.install(build)
        self.assertEqual(renamed, 1)
        warning = "".join(line for line in log if "shortened to fit 30" in line)
        self.assertTrue(warning, log)
        self.assertIn("PFS3 partition avoids this", warning,
                      "on FFS there is somewhere better to go")

    def test_pfs3_is_not_told_to_switch_to_pfs3(self):
        """Advice to use the file system already in use is no advice at all."""
        def build(source: Path):
            (source / ("a-name-far-longer-than-even-pfs3-will-hold" * 4)
             ).write_bytes(b"a")
        _listing, _copied, renamed, log = self.install(build, blocks=60000,
                                                       pfs3_volume=True)
        self.assertEqual(renamed, 1)
        warning = "".join(line for line in log if "shortened to fit" in line)
        self.assertTrue(warning, log)
        self.assertNotIn("PFS3 partition avoids this", warning)

    def test_the_same_name_stored_two_ways_is_explained_as_such(self):
        """Both spellings print identically, so the message has to say more.

        PiMiga's Locale drawer holds "espa\xf1a.country" twice: once with the
        bytes an Amiga writes, once as UTF-8. Reporting that one cannot be told
        from the other reads as nonsense when the two are shown the same way.
        """
        def build(source: Path):
            (source / os.fsdecode(b"espa\xf1a.country")).write_bytes(b"the 2018 one")
            (source / "espa\u00f1a.country").write_bytes(b"the 2021 one")
        listing, _copied, _renamed, log = self.install(build)
        self.assertEqual(list(listing), ["espa\u00f1a.country"], listing)
        message = "".join(line for line in log if "left out" in line)
        self.assertIn("stores this name twice", message)
        self.assertIn("ISO-8859-1", message)
        self.assertIn("UTF-8", message)

    def test_an_iso_8859_1_name_reaches_the_volume_unchanged(self):
        def build(source: Path):
            (source / os.fsdecode(b"fran\xe7ais")).write_bytes(b"a")
        listing, _copied, renamed, log = self.install(build)
        self.assertEqual(list(listing), ["fran\u00e7ais"])
        self.assertEqual(renamed, 0)
        self.assertFalse([line for line in log if "->" in line], log)


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
