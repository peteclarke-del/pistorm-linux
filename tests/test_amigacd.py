"""AmigaOS 3.5 and 3.9: the CD reader, the disc layouts, and the BoingBags.

The ISO fixtures are built with ``genisoimage`` rather than by a writer of our
own.  That is deliberate, and it is the same reason the FFS writer is checked
against ``amitools``: a reader and a writer that share an assumption agree with
each other perfectly and are both wrong.  Where ``genisoimage`` is missing the
tests that need it skip rather than pretend.

The real discs are used when they are present, and skipped when they are not,
in the same way the file system tests use the Workbench disks in ``samples/``.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import (amigacd, bbupdate,        # noqa: E402
                                 boingbag, iso9660)
from pistorm_imager.core.machines import (Accelerator, Cpu,          # noqa: E402
                                          MACHINES_BY_KEY)
from pistorm_imager.core.util import Progress                        # noqa: E402

HAVE_GENISOIMAGE = shutil.which("genisoimage") or shutil.which("mkisofs")
HAVE_7Z = shutil.which("7z") or shutil.which("7za")

#  The user's own discs, if this is the machine they live on.  Nothing here
#  depends on them; they are the check that the synthetic shapes match reality.
REAL_DISCS = {
    "3.5": Path.home() / "Downloads" / "AmigaOS3.5.iso",
    "3.9": Path.home() / "Downloads" / "AmigaOS39.iso",
}


def make_iso(tree: Path, output: Path, *, joliet: bool = False,
             rock_ridge: bool = False, volume: str = "TestVol") -> Path:
    """Master an ISO from a directory, using an independent implementation."""
    command = [HAVE_GENISOIMAGE, "-quiet", "-V", volume, "-o", str(output)]
    if joliet:
        command.append("-J")
    if rock_ridge:
        command.append("-R")
    command.append(str(tree))
    subprocess.run(command, check=True)
    return output


def write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TheIsoReader(unittest.TestCase):
    """Reading an ISO 9660 image, in the three dialects that turn up."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_GENISOIMAGE:
            raise unittest.SkipTest("genisoimage is not installed")
        cls.scratch = Path(tempfile.mkdtemp(prefix="pistorm-iso-test-"))
        tree = cls.scratch / "tree"
        write(tree / "Workbench3.9" / "Libs" / "icon.library", b"hello")
        write(tree / "Workbench3.9" / "WBStartup" / "AmiDock", b"dock")
        write(tree / "Workbench3.9" / "MixedCase Name.info", b"icon")
        cls.plain = make_iso(tree, cls.scratch / "plain.iso")
        cls.joliet = make_iso(tree, cls.scratch / "joliet.iso", joliet=True)
        cls.rr = make_iso(tree, cls.scratch / "rr.iso", rock_ridge=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def test_the_volume_name_is_read(self):
        with iso9660.IsoImage.open(self.plain) as iso:
            self.assertEqual(iso.volume_name, "TestVol")

    def test_rock_ridge_restores_the_real_spelling(self):
        """The bug this would otherwise ship: an all-upper-case Workbench.

        Plain ISO 9660 upper-cases and truncates, so a reader that stopped
        there would put AMIDOCK in WBStartup - and Workbench draws an icon's
        label from the file name.
        """
        with iso9660.IsoImage.open(self.plain) as iso:
            plain = {e.name for e in iso.listdir(iso.root())}
        with iso9660.IsoImage.open(self.rr) as iso:
            self.assertFalse(iso.joliet, "this disc has no Joliet to fall back on")
            real = {e.name for e in iso.listdir(iso.root())}
            names = {e.name for e in
                     iso.listdir(iso.find("Workbench3.9/WBStartup"))}
        self.assertNotIn("Workbench3.9", plain)      # proves the fixture bites
        self.assertIn("Workbench3.9", real)
        self.assertIn("AmiDock", names)

    def test_joliet_is_preferred_and_gives_the_real_spelling(self):
        with iso9660.IsoImage.open(self.joliet) as iso:
            self.assertTrue(iso.joliet)
            self.assertIn("MixedCase Name.info",
                          {e.name for e in iso.listdir(iso.find("Workbench3.9"))})

    def test_the_version_suffix_is_stripped(self):
        with iso9660.IsoImage.open(self.plain) as iso:
            for entry in iso.walk():
                self.assertNotIn(";", entry[1].name)

    def test_files_read_back_byte_for_byte(self):
        for image in (self.plain, self.joliet, self.rr):
            with iso9660.IsoImage.open(image) as iso:
                entry = next(e for _p, e in iso.walk()
                             if not e.is_dir and e.size == 5)
                self.assertEqual(iso.read(entry), b"hello")

    def test_find_is_case_insensitive(self):
        with iso9660.IsoImage.open(self.rr) as iso:
            for spelling in ("Workbench3.9/Libs", "workbench3.9/libs",
                             "WORKBENCH3.9/LIBS"):
                self.assertIsNotNone(iso.find(spelling), spelling)

    def test_a_missing_path_is_none_not_an_error(self):
        with iso9660.IsoImage.open(self.rr) as iso:
            self.assertIsNone(iso.find("Workbench3.9/NotThere/AtAll"))

    def test_extract_writes_the_same_bytes(self):
        with iso9660.IsoImage.open(self.rr) as iso:
            entry = iso.find("Workbench3.9/Libs/icon.library")
            out = iso.extract(entry, self.scratch / "out" / "icon.library")
            self.assertEqual(out.read_bytes(), b"hello")

    def test_something_that_is_not_an_iso_is_refused(self):
        junk = self.scratch / "junk.iso"
        junk.write_bytes(b"\0" * (40 * iso9660.SECTOR))
        self.assertFalse(iso9660.is_iso(junk))
        self.assertTrue(iso9660.is_iso(self.plain))
        with self.assertRaises(iso9660.Iso9660Error):
            iso9660.IsoImage.open(junk)


class RecognisingAnAmigaOsDisc(unittest.TestCase):
    """Telling a 3.5 disc from a 3.9 one, and finding what to copy off it."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_GENISOIMAGE:
            raise unittest.SkipTest("genisoimage is not installed")
        cls.scratch = Path(tempfile.mkdtemp(prefix="pistorm-cd-test-"))
        cls.discs = {}
        for release in amigacd.RELEASES:
            tree = cls.scratch / release.key
            for layer in release.layers:
                write(tree / layer.source / "afile", b"content")
            cls.discs[release.key] = make_iso(
                tree, cls.scratch / f"{release.key}.iso",
                rock_ridge=True, volume=release.volume)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def test_each_release_is_recognised_by_its_volume_name(self):
        for key, image in self.discs.items():
            match = amigacd.identify(image)
            self.assertIsNotNone(match.release, key)
            self.assertEqual(match.release.key, key)
            self.assertTrue(match.usable, match.label)

    def test_every_layer_is_found_on_a_disc_that_has_them_all(self):
        for key, image in self.discs.items():
            match = amigacd.identify(image)
            self.assertEqual(match.missing, (), f"{key}: {match.missing}")
            self.assertEqual(len(match.present),
                             len(amigacd.RELEASES_BY_KEY[key].layers))

    def test_a_relabelled_disc_is_recognised_by_its_layout(self):
        """A re-mastered disc keeps its trees and loses its name."""
        release = amigacd.RELEASES_BY_KEY["3.9"]
        tree = self.scratch / "relabelled"
        for layer in release.layers:
            write(tree / layer.source / "afile", b"content")
        image = make_iso(tree, self.scratch / "relabelled.iso",
                         rock_ridge=True, volume="SOMEBODYS BACKUP")
        match = amigacd.identify(image)
        self.assertIsNotNone(match.release)
        self.assertEqual(match.release.key, "3.9")

    def test_a_disc_missing_a_required_tree_is_not_usable(self):
        release = amigacd.RELEASES_BY_KEY["3.9"]
        tree = self.scratch / "gutted"
        #  Everything except the Workbench trees, which are the required ones.
        for layer in release.layers:
            if not layer.required:
                write(tree / layer.source / "afile", b"content")
        write(tree / "OS-Version3.9" / "marker", b"x")
        image = make_iso(tree, self.scratch / "gutted.iso",
                         rock_ridge=True, volume=release.volume)
        match = amigacd.identify(image)
        self.assertFalse(match.usable)
        self.assertTrue(any(layer.required for layer in match.missing))

    def test_an_ordinary_disc_is_not_an_amigaos_one(self):
        tree = self.scratch / "other"
        write(tree / "Something" / "else", b"x")
        image = make_iso(tree, self.scratch / "other.iso", rock_ridge=True,
                         volume="MUSIC")
        match = amigacd.identify(image)
        self.assertIsNone(match.release)
        self.assertFalse(match.usable)

    def test_a_file_that_is_not_a_disc_reports_rather_than_raises(self):
        junk = self.scratch / "notadisc.iso"
        junk.write_bytes(b"\0" * 1024)
        match = amigacd.identify(junk)
        self.assertTrue(match.error)
        self.assertFalse(match.usable)

    def test_the_35_disc_lays_31_down_before_35(self):
        """3.5's own Workbench tree is a delta and cannot stand alone.

        It carries no S and no WBStartup, so if the 3.1 trees the disc also
        supplies are not copied first the drive has no Startup-Sequence.
        """
        layers = amigacd.RELEASES_BY_KEY["3.5"].layers
        order = {layer.source: layer.order for layer in layers}
        self.assertLess(order["OS-Version3.1/Workbench3.1"],
                        order["OS-Version3.5/Workbench"])

    def test_the_39_disc_lays_35_down_before_39(self):
        layers = amigacd.RELEASES_BY_KEY["3.9"].layers
        order = {layer.source: layer.order for layer in layers}
        self.assertLess(order["OS-Version3.9/Workbench3.5"],
                        order["OS-Version3.9/Workbench3.9"])

    def test_backdrops_go_where_the_disc_says_and_not_where_it_looks(self):
        """Read out of the disc's own installer, not guessed.

        Extras/Backdrops lands in Prefs/Presets/Backdrops.  "Backdrops" is the
        obvious answer and the wrong one, and a wrong destination makes a
        system that looks installed.
        """
        for release in amigacd.RELEASES:
            backdrops = [layer for layer in release.layers
                         if layer.source.endswith("Extras/Backdrops")]
            self.assertEqual(len(backdrops), 1, release.key)
            self.assertEqual(backdrops[0].destination,
                             "Prefs/Presets/Backdrops")


class TheProcessorAndKickstartGate(unittest.TestCase):
    """What may run 3.5 and 3.9, and what may not."""

    def setUp(self):
        self.release = amigacd.RELEASES_BY_KEY["3.9"]

    def check(self, machine_key, accelerator, kickstart=40, card_cpu=None):
        return amigacd.requirements(
            self.release, MACHINES_BY_KEY[machine_key], accelerator,
            card_cpu=card_cpu, kickstart_version=kickstart)

    def test_a_stock_68000_machine_is_refused(self):
        problems = self.check("a500", Accelerator.STOCK)
        self.assertTrue(problems)
        self.assertIn("68020", problems[0])

    def test_an_accelerated_machine_is_allowed(self):
        self.assertEqual(self.check("a500", Accelerator.ACCELERATOR), [])

    def test_a_pistorm_is_allowed_on_every_machine(self):
        for machine in MACHINES_BY_KEY:
            self.assertEqual(self.check(machine, Accelerator.PISTORM), [],
                             machine)

    def test_a_stock_a1200_is_allowed_because_it_has_an_020(self):
        self.assertEqual(self.check("a1200", Accelerator.STOCK), [])

    def test_an_accelerator_that_is_only_an_010_is_refused(self):
        problems = self.check("a500", Accelerator.ACCELERATOR,
                              card_cpu=Cpu.M68010)
        self.assertTrue(problems)

    def test_the_wrong_kickstart_is_refused_whatever_the_processor(self):
        for version in (37, 39, 46, 47):
            problems = self.check("a1200", Accelerator.PISTORM,
                                  kickstart=version)
            self.assertTrue(problems, version)
            self.assertIn("Kickstart", problems[0])

    def test_no_kickstart_chosen_yet_is_not_a_complaint_from_here(self):
        """Not having chosen is a different complaint, and not this one's."""
        self.assertEqual(self.check("a1200", Accelerator.PISTORM,
                                    kickstart=None), [])

    def test_both_faults_are_reported_together(self):
        problems = self.check("a500", Accelerator.STOCK, kickstart=37)
        self.assertEqual(len(problems), 2)

    def test_the_processor_rule_is_recorded_even_though_pistorm_clears_it(self):
        """The requirement belongs to AmigaOS, not to today's accelerator.

        A PiStorm always satisfies it, so this check can only ever refuse a
        stock machine - but the rule is still the software's, and it is stated
        here rather than left implicit in the fact that nothing violates it.
        """
        self.assertEqual(self.release.needs_cpu, Cpu.M68020)
        machine = MACHINES_BY_KEY["a500"]
        self.assertEqual(machine.cpu_fitted(Accelerator.PISTORM), Cpu.M68040)
        self.assertEqual(machine.cpu_fitted(Accelerator.STOCK), Cpu.M68000)


class TheBoingBags(unittest.TestCase):
    """The updates, and which of them this project can apply on its own."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix="pistorm-bb-test-"))
        cls.roots = {}
        for bag in boingbag.BAGS:
            root = cls.scratch / "packs" / bag.root
            for layer in bag.layers:
                write(root / layer.source / "afile", b"content")
            for variant in bag.variants:
                for _rule, source in variant.candidates:
                    write(root / source, b"binary")
            cls.roots[bag.key] = root

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def test_the_packs_are_ordered_as_they_were_published(self):
        for release in ("3.5", "3.9"):
            bags = boingbag.for_release(release)
            self.assertEqual([b.order for b in bags],
                             sorted(b.order for b in bags))

    def test_a_packs_drawer_is_found_however_deeply_it_was_nested(self):
        """BB1-4.lha carries three packs side by side, one level down."""
        for bag in boingbag.BAGS:
            nested = self.scratch / "deep" / "and" / "deeper" / bag.root
            nested.mkdir(parents=True, exist_ok=True)
            found = boingbag.find_root(self.scratch / "deep", bag)
            self.assertEqual(found, nested, bag.key)

    def test_a_pistorm_gets_the_68040_fpu_build(self):
        bag = boingbag.BAGS_BY_KEY["3.9-34"]
        root = self.roots["3.9-34"]
        mpega = next(v for v in bag.variants if v.label == "mpega")
        chosen = boingbag.choose_variant(mpega, MACHINES_BY_KEY["a1200"],
                                         Cpu.M68040, True, root)
        self.assertTrue(chosen.endswith("mpega040FPU.library"), chosen)

    def test_without_an_fpu_the_plain_build_is_taken(self):
        bag = boingbag.BAGS_BY_KEY["3.9-34"]
        root = self.roots["3.9-34"]
        mpega = next(v for v in bag.variants if v.label == "mpega")
        chosen = boingbag.choose_variant(mpega, MACHINES_BY_KEY["a1200"],
                                         Cpu.M68040, False, root)
        self.assertTrue(chosen.endswith("mpega040.library"), chosen)

    def test_a_68060_gets_the_060_xadmaster(self):
        bag = boingbag.BAGS_BY_KEY["3.9-34"]
        root = self.roots["3.9-34"]
        xad = next(v for v in bag.variants if v.label == "xadmaster")
        self.assertTrue(boingbag.choose_variant(
            xad, MACHINES_BY_KEY["a1200"], Cpu.M68060, True, root)
            .endswith("xadmaster_060.library"))
        self.assertTrue(boingbag.choose_variant(
            xad, MACHINES_BY_KEY["a1200"], Cpu.M68040, True, root)
            .endswith("xadmaster_020.library"))

    def test_a_machine_with_no_ide_gets_no_ide_driver(self):
        """Putting an A1200's scsi.device on an A500 would invent hardware."""
        bag = boingbag.BAGS_BY_KEY["3.9-34"]
        root = self.roots["3.9-34"]
        ide = next(v for v in bag.variants if v.label == "IDE driver")
        for machine in ("a500", "a500plus", "a1000", "a2000", "raspi"):
            self.assertEqual(
                boingbag.choose_variant(ide, MACHINES_BY_KEY[machine],
                                        Cpu.M68040, True, root), "", machine)
        for machine in ("a600", "a1200"):
            self.assertTrue(
                boingbag.choose_variant(ide, MACHINES_BY_KEY[machine],
                                        Cpu.M68040, True, root), machine)

    def test_a_candidate_whose_file_is_absent_is_passed_over(self):
        """The packs do not all agree with their own scripts.

        The community release ships one scsi_A600_A1200.device where its
        Installer still names an A600 file and an A1200 file separately, so
        whichever spelling is actually present has to win.
        """
        bag = boingbag.BAGS_BY_KEY["3.9-34"]
        ide = next(v for v in bag.variants if v.label == "IDE driver")
        sparse = self.scratch / "sparse"
        write(sparse / "Files2/Devs/scsi_A600_A1200.device", b"x")
        chosen = boingbag.choose_variant(ide, MACHINES_BY_KEY["a1200"],
                                         Cpu.M68040, True, sparse)
        self.assertTrue(chosen.endswith("scsi_A600_A1200.device"), chosen)

        other = self.scratch / "other"
        write(other / "Files2/Devs/scsi_A1200.device", b"x")
        chosen = boingbag.choose_variant(ide, MACHINES_BY_KEY["a1200"],
                                         Cpu.M68040, True, other)
        self.assertTrue(chosen.endswith("scsi_A1200.device"), chosen)

    def test_a_variant_never_lands_under_its_build_specific_name(self):
        """Every variant is renamed to the name the system looks up."""
        for bag in boingbag.BAGS:
            for variant in bag.variants:
                self.assertTrue(variant.newname)
                for _rule, source in variant.candidates:
                    self.assertNotEqual(Path(source).name, variant.newname)

    @unittest.skipUnless(HAVE_7Z, "7z is needed to build an encrypted archive")
    def test_a_locked_payload_is_detected_and_its_contents_named(self):
        """Named, not counted: "some fixes were skipped" is not actionable."""
        bag = boingbag.BAGS_BY_KEY["3.9-1"]
        root = self.scratch / "locked"
        root.mkdir(parents=True, exist_ok=True)
        stage = self.scratch / "stage"
        write(stage / "Libs" / "icon.library", b"payload")
        write(stage / "C" / "Version", b"payload")
        #  From inside the staging directory, so the names in the archive are
        #  the system-relative ones a real payload carries - "Libs/icon.library"
        #  rather than a path off this machine's temporary directory.  7z adds
        #  a .zip suffix to a name that has none, and the real payload is
        #  called "AmigaOS-Update" with no extension at all, so it is put back.
        built = root / "payload.zip"
        subprocess.run([HAVE_7Z, "a", "-tzip", "-psecret", str(built), "."],
                       cwd=stage, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        built.rename(root / bag.locked_payloads[0])
        self.assertTrue(boingbag.is_locked(root, bag))
        names = boingbag.locked_entries(root, bag)
        self.assertTrue(any(n.endswith("icon.library") for n in names), names)
        progress = Progress()
        self.assertEqual(sorted(boingbag.report_skipped(bag, root, progress)),
                         sorted(names))

    def test_a_pack_with_no_locked_payload_says_so(self):
        for key in ("3.5-1", "3.5-2", "3.9-34"):
            bag = boingbag.BAGS_BY_KEY[key]
            self.assertEqual(bag.locked_payloads, ())
            self.assertFalse(boingbag.is_locked(self.roots[key], bag))
            self.assertEqual(boingbag.locked_entries(self.roots[key], bag), [])

    def test_boingbag_39_2_is_entirely_locked(self):
        """It carries no plain system files at all.

        C and Installer are the Updater's own tooling and Manuals is
        documentation, so a build that cannot run Updater applies nothing of
        it - which is worth stating rather than discovering.
        """
        bag = boingbag.BAGS_BY_KEY["3.9-2"]
        self.assertEqual(bag.layers, ())
        self.assertEqual(len(bag.locked_payloads), 2)


class ApplyingALockedUpdate(unittest.TestCase):
    """The emulator pass, and what it must not leave behind if it fails."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_7Z:
            raise unittest.SkipTest("7z is needed to build an encrypted archive")
        cls.scratch = Path(tempfile.mkdtemp(prefix="pistorm-locked-test-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def make(self, name):
        """A staged system, and a pack whose locked payload replaces part of it."""
        root = self.scratch / name
        staged = root / "System"
        write(staged / "Libs" / "icon.library", b"from the CD")
        write(staged / "C" / "IPrefs", b"from the CD")
        write(staged / "Libs" / "other.library", b"not in the update")
        pack = root / "Pack"
        pack.mkdir(parents=True, exist_ok=True)
        stage = root / "payload"
        write(stage / "Libs" / "icon.library", b"from the update")
        write(stage / "C" / "IPrefs", b"from the update")
        built = pack / "payload.zip"
        subprocess.run([HAVE_7Z, "a", "-tzip", "-psecret", str(built), "."],
                       cwd=stage, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        built.rename(pack / "AmigaOS-Update")
        return staged, pack

    def test_the_files_the_update_will_write_are_moved_out_of_its_way(self):
        """XAD will not write over a file that is already there.

        Every file a BoingBag carries is one the CD has just installed, so
        without this every single one collides and the update does nothing.
        """
        staged, pack = self.make("clearing")
        bag = boingbag.BAGS_BY_KEY["3.9-1"]
        keep = staged.parent / "replaced"
        moved = bbupdate._clear_targets(staged, pack, bag, keep, Progress())
        self.assertEqual(moved, 2)
        self.assertFalse((staged / "Libs" / "icon.library").exists())
        self.assertFalse((staged / "C" / "IPrefs").exists())
        #  A file the update does not carry is left exactly alone.
        self.assertEqual((staged / "Libs" / "other.library").read_bytes(),
                         b"not in the update")

    def test_a_failed_update_leaves_the_system_whole(self):
        """Moved aside, not deleted.

        An update that does not finish would otherwise leave the system
        *missing* the files it was meant to improve - a worse card than the one
        that was there before, and one that still looks like a good build.
        """
        staged, pack = self.make("failing")
        bag = boingbag.BAGS_BY_KEY["3.9-1"]
        keep = staged.parent / "replaced"
        bbupdate._clear_targets(staged, pack, bag, keep, Progress())
        restored = bbupdate._restore_targets(staged, keep)
        self.assertEqual(restored, 2)
        self.assertEqual((staged / "Libs" / "icon.library").read_bytes(),
                         b"from the CD")
        self.assertEqual((staged / "C" / "IPrefs").read_bytes(), b"from the CD")
        self.assertFalse(keep.exists())

    def test_what_the_update_did_write_is_kept(self):
        """Restoring must not undo the update's own work."""
        staged, pack = self.make("succeeding")
        bag = boingbag.BAGS_BY_KEY["3.9-1"]
        keep = staged.parent / "replaced"
        bbupdate._clear_targets(staged, pack, bag, keep, Progress())
        #  Stand in for the Updater having written one of the two.
        write(staged / "Libs" / "icon.library", b"from the update")
        restored = bbupdate._restore_targets(staged, keep)
        self.assertEqual(restored, 1)
        self.assertEqual((staged / "Libs" / "icon.library").read_bytes(),
                         b"from the update")
        self.assertEqual((staged / "C" / "IPrefs").read_bytes(), b"from the CD")

    def test_the_startup_hook_is_taken_back_off(self):
        """The card must not carry an Updater call that would run on the Amiga."""
        staged, _pack = self.make("hook")
        write(staged / "S" / "User-Startup", b"; the system's own\n")
        bag = boingbag.BAGS_BY_KEY["3.9-1"]
        bbupdate._install_hook(staged, bag)
        body = (staged / "S" / "User-Startup").read_text()
        self.assertIn("Updater", body)
        self.assertIn("; the system's own", body)
        bbupdate._remove_hook(staged)
        self.assertEqual((staged / "S" / "User-Startup").read_text(),
                         "; the system's own\n")
        self.assertFalse((staged / bbupdate.MARKER).exists())

    def test_a_system_with_no_user_startup_does_not_gain_one(self):
        staged, _pack = self.make("nostartup")
        bag = boingbag.BAGS_BY_KEY["3.9-1"]
        bbupdate._install_hook(staged, bag)
        self.assertTrue((staged / "S" / "User-Startup").exists())
        bbupdate._remove_hook(staged)
        self.assertFalse((staged / "S" / "User-Startup").exists())

    def test_a_confined_emulator_cannot_see_the_usual_places(self):
        """A snap sees neither /tmp nor the hidden parts of the home."""
        self.assertFalse(bbupdate._reachable(Path("/tmp/anything"), Path("/")))
        self.assertFalse(bbupdate._reachable(Path.home() / ".cache" / "x",
                                             Path("/")))
        self.assertTrue(bbupdate._reachable(Path.home() / "Visible" / "x",
                                            Path("/")))


class WhatThePlanSays(unittest.TestCase):
    """The plan is the last thing read before Write, so it must be true."""

    def plan(self, **overrides):
        from pistorm_imager.core import builder, presets           # noqa: PLC0415
        from pistorm_imager.core import machines as m              # noqa: PLC0415
        config = builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target="/tmp/plan.img",
            boot_size=512 * 1024 * 1024, machine_key="a500ecs",
            amiga_partitions=[builder.AmigaPartitionSpec(
                name="DH0", size=None, dostype="PFS3", bootable=True)],
            **overrides)
        return presets.describe_machine_setup(
            config, m.MACHINES_BY_KEY["a500ecs"], m.Display.NATIVE,
            presets.Detected())

    def test_a_cd_install_is_named_rather_than_left_as_a_key(self):
        """"System: cd" was the raw value, for want of a label."""
        said = self.plan(system_source="cd", os_cd="/tmp/AmigaOS39.iso")
        self.assertIn("System: AmigaOS installed from its CD image", said)
        self.assertNotIn("System: cd", said)

    def test_a_cd_fills_the_boot_drive_rather_than_leaving_it_empty(self):
        said = self.plan(system_source="cd", os_cd="/tmp/AmigaOS39.iso")
        self.assertIn("AmigaOS installed from AmigaOS39.iso", said)
        self.assertNotIn("DH0: the rest of the card, PFS3 - left empty", said)
        self.assertNotIn("Nothing will be installed onto the boot drive", said)

    def test_emu68_is_not_promised_when_it_is_not_being_installed(self):
        """The plan said "FAT32 with Emu68" whether or not it was."""
        with_it = self.plan(install_emu68=True)
        self.assertIn("FAT32 with Emu68", with_it)
        without = self.plan(install_emu68=False)
        self.assertNotIn("FAT32 with Emu68", without)
        self.assertIn("without Emu68", without)

    def test_a_missing_kickstart_is_only_a_fault_if_emu68_wants_one(self):
        """With no Emu68 nothing on the card maps a ROM."""
        self.assertIn("Emu68 will not start without one",
                      self.plan(install_emu68=True, kickstart_path=""))
        self.assertNotIn("Emu68 will not start without one",
                         self.plan(install_emu68=False, kickstart_path=""))

    def test_the_plan_names_what_provides_the_processor(self):
        """Naming a PiStorm board on a machine set to an accelerator card
        described hardware that is not there, in the plan's first line."""
        self.assertIn("with PiStorm (classic)",
                      self.plan(accelerator="pistorm").splitlines()[0])
        accelerated = self.plan(accelerator="accelerator",
                                accelerator_cpu="68030").splitlines()[0]
        self.assertIn("an accelerator (MC68030)", accelerated)
        self.assertNotIn("PiStorm", accelerated)
        stock = self.plan(accelerator="stock").splitlines()[0]
        self.assertIn("its own MC68000", stock)
        self.assertNotIn("PiStorm", stock)


class AgainstTheRealDiscs(unittest.TestCase):
    """The synthetic shapes above are only worth as much as they resemble.

    These run when the discs are on this machine and skip when they are not,
    the same way the file system tests use the Workbench disks in samples/.
    """

    def disc(self, key):
        path = REAL_DISCS[key]
        if not path.is_file():
            self.skipTest(f"{path.name} is not on this machine")
        return path

    def test_the_real_35_disc_is_recognised_and_complete(self):
        match = amigacd.identify(self.disc("3.5"))
        self.assertEqual(match.release.key, "3.5")
        self.assertTrue(match.usable, match.label)
        self.assertEqual(match.missing, ())
        self.assertGreater(match.files, 1000)

    def test_the_real_39_disc_is_recognised_and_complete(self):
        match = amigacd.identify(self.disc("3.9"))
        self.assertEqual(match.release.key, "3.9")
        self.assertTrue(match.usable, match.label)
        self.assertEqual(match.missing, ())
        self.assertGreater(match.files, 2000)

    def test_the_real_39_disc_needs_rock_ridge_to_read_correctly(self):
        """The disc that proved this mattered."""
        with iso9660.IsoImage.open(self.disc("3.9")) as iso:
            self.assertFalse(iso.joliet)
            names = {e.name for e in
                     iso.listdir(iso.find("OS-Version3.9/Workbench3.9/WBStartup"))}
        self.assertIn("AmiDock", names)
        self.assertNotIn("AMIDOCK", names)

    def test_the_real_35_disc_reads_through_joliet(self):
        with iso9660.IsoImage.open(self.disc("3.5")) as iso:
            self.assertTrue(iso.joliet)
            names = {e.name for e in
                     iso.listdir(iso.find("OS-Version3.5/Workbench/Libs"))}
        self.assertIn("amigaguide.library", names)


if __name__ == "__main__":
    unittest.main()
