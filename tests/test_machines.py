"""Tests for target machine profiles and the hardware-driven setup."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import builder, emu68, jobs, machines, presets  # noqa: E402
from pistorm_imager.core.util import GIB, MIB  # noqa: E402

Display = machines.Display


class TestProfiles(unittest.TestCase):
    def test_every_machine_names_a_real_emu68_build(self):
        keys = {v.key for v in emu68.VARIANTS}
        for machine in machines.MACHINES:
            self.assertIn(machine.board, keys, machine.label)

    def test_the_68000_machines_use_the_classic_board(self):
        for key in ("a500", "a500plus", "a1000", "a2000"):
            self.assertEqual(machines.MACHINES_BY_KEY[key].board, "pistorm")

    def test_only_the_a1200_is_aga(self):
        aga = {m.key for m in machines.MACHINES if m.aga}
        self.assertEqual(aga, {"a1200"})

    def test_only_the_a500_family_has_trapdoor_ram(self):
        trapdoor = {m.key for m in machines.MACHINES if m.trapdoor_ram}
        self.assertEqual(trapdoor, {"a500", "a500plus"})


class TestBootOptions(unittest.TestCase):
    def machine(self, key: str) -> machines.Machine:
        return machines.MACHINES_BY_KEY[key]

    def test_chip_slowdown_on_for_the_older_chipsets(self):
        for key in ("a500", "a500plus", "a600", "a2000"):
            options = machines.boot_options(self.machine(key), Display.NATIVE)
            self.assertTrue(options.chip_slowdown, key)

    def test_vbr_move_is_never_on_by_default(self):
        """It is faster but wrecks floppy-loaded games, per Emu68's own docs."""
        for machine in machines.MACHINES:
            for display in Display:
                options = machines.boot_options(machine, display)
                self.assertFalse(options.vbr_move, machine.key)

    def test_native_display_does_not_force_an_hdmi_mode(self):
        options = machines.boot_options(self.machine("a500"), Display.NATIVE,
                                        hdmi=(2, 82))
        self.assertTrue(options.hdmi_automatic)
        self.assertIsNone(options.vc4_mem)

    def test_rtg_display_sets_the_hdmi_mode_and_video_memory(self):
        options = machines.boot_options(self.machine("a1200"), Display.RTG_HDMI,
                                        hdmi=(2, 82))
        self.assertEqual((options.hdmi_group, options.hdmi_mode), (2, 82))
        self.assertEqual(options.vc4_mem, 64)

    def test_framethrower_enables_unicam(self):
        options = machines.boot_options(self.machine("a500"),
                                        Display.FRAMETHROWER)
        self.assertTrue(options.unicam)
        self.assertIn("unicam.boot", options.cmdline())

    def test_trapdoor_option_only_when_asked(self):
        machine = self.machine("a500")
        plain = machines.boot_options(machine, Display.NATIVE)
        self.assertNotIn("move_slow_to_chip", plain.cmdline())
        with_trapdoor = machines.boot_options(machine, Display.NATIVE,
                                              trapdoor_to_chip=True)
        self.assertIn("move_slow_to_chip", with_trapdoor.cmdline())


class TestAdvice(unittest.TestCase):
    def test_native_on_a_non_aga_machine_warns_about_screen_modes(self):
        notes = " ".join(machines.advice(machines.MACHINES_BY_KEY["a500"],
                                         Display.NATIVE))
        self.assertIn("256-colour", notes)
        self.assertIn("RTG", notes)

    def test_rtg_mentions_the_hdmi_sink_requirement(self):
        notes = " ".join(machines.advice(machines.MACHINES_BY_KEY["a1200"],
                                         Display.RTG_HDMI))
        self.assertIn("before powering on", notes)


class TestContentSelection(unittest.TestCase):
    def test_non_aga_machines_leave_out_aga_collections(self):
        for key in ("a500", "a500plus", "a600", "a1000", "a2000"):
            excluded = presets.excluded_for(machines.MACHINES_BY_KEY[key])
            self.assertIn("WHDLOAD/AGA", excluded, key)
            self.assertIn("WHDLOAD/CD32", excluded, key)

    def test_an_a1200_gets_everything(self):
        self.assertEqual(presets.excluded_for(machines.MACHINES_BY_KEY["a1200"]),
                         [])


BUNDLED_HANDLER = Path(__file__).resolve().parent.parent / "samples" / "pfs3aio"


class TestPfs3Handler(unittest.TestCase):
    """PFS3 can be created here, but AmigaOS still cannot mount it without the
    handler, so one has to be found and embedded rather than asked for."""

    def test_version_is_read_from_the_binary(self):
        binary = b"junk\x00$VER: Professional-File-System-III 19.2 PFS3AIO\x00"
        self.assertEqual(builder.handler_version(binary), (19 << 16) | 2)

    def test_missing_version_string_is_zero(self):
        self.assertEqual(builder.handler_version(b"no version anywhere"), 0)

    @unittest.skipUnless(BUNDLED_HANDLER.is_file(), "no bundled PFS3 handler")
    def test_the_bundled_handler_is_a_real_amiga_binary(self):
        data = BUNDLED_HANDLER.read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x03\xf3", "hunk header")
        version = builder.handler_version(data)
        self.assertGreater(version >> 16, 0, "should carry a version")

    @unittest.skipUnless(BUNDLED_HANDLER.is_file(), "no bundled PFS3 handler")
    def test_it_is_found_without_being_asked_for(self):
        found = presets.find_pfs3_handler()
        self.assertIsNotNone(found, "a handler ships with the project")
        self.assertEqual(Path(found[0]).read_bytes()[:4], b"\x00\x00\x03\xf3")


class TestImageInspection(unittest.TestCase):
    """An imported image is examined, so the user is told what it brings."""

    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-inspect-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder

    def make_volume(self, entries: dict) -> Path:
        from pistorm_imager.core import amigafs
        path = self.scratch() / "sys.hdf"
        blocks = 40000
        handle = open(path, "w+b")
        self.addCleanup(handle.close)
        handle.truncate(blocks * amigafs.BLOCK)
        volume = amigafs.VolumeWriter.format(handle, 0, blocks, "System")
        for location, data in entries.items():
            parts = location.split("/")
            parent = volume.makedirs("/".join(parts[:-1])) if len(parts) > 1 \
                else volume.root
            volume.write_file(parent, parts[-1], data)
        volume.close()
        handle.flush()
        return path

    def test_a_bootable_system_is_recognised(self):
        path = self.make_volume({"S/Startup-Sequence": b"LoadWB\n",
                                 "C/LoadWB": b"x", "C/WHDLoad": b"x"})
        found = presets.inspect_image_system(path)
        self.assertTrue(found.bootable)
        self.assertFalse(found.needs_floppies)
        self.assertIn("has WHDLoad", found.describe())

    def test_a_data_only_drive_says_floppies_are_needed(self):
        path = self.make_volume({"Docs/readme.txt": b"nothing bootable here"})
        found = presets.inspect_image_system(path)
        self.assertFalse(found.bootable)
        self.assertTrue(found.needs_floppies)
        self.assertIn("no operating system", found.describe())

    def test_an_unreadable_file_is_reported_not_raised(self):
        path = self.scratch() / "junk.hdf"
        path.write_bytes(b"not an amiga volume at all" * 100)
        found = presets.inspect_image_system(path)
        self.assertTrue(found.error)
        self.assertIn("could not be examined", found.describe())


class TestSavedSessions(unittest.TestCase):
    """A saved setup has to survive being reloaded, or it saves nobody time."""

    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-session-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder

    def test_config_and_choices_round_trip(self):
        config = builder.BuildConfig(target="/tmp/card.img", variant="pistorm",
                                     boot_size=128 * 1024 * 1024)
        choices = {"machine": "a600", "display": "RTG_HDMI",
                   "pimiga_folder": "/somewhere/pimiga", "trapdoor": True}
        path = self.scratch() / "session.json"
        jobs.save_session(config, choices, path)
        back, restored = jobs.load_session(path)
        self.assertEqual(back, config)
        self.assertEqual(restored, choices)

    def test_a_file_without_choices_still_loads(self):
        """Settings saved before the choices were recorded must not break."""
        config = builder.BuildConfig(target="/tmp/card.img")
        path = self.scratch() / "old.json"
        jobs.save(config, path)
        back, choices = jobs.load_session(path)
        self.assertEqual(back, config)
        self.assertEqual(choices, {})

    def test_a_corrupt_file_is_refused_clearly(self):
        path = self.scratch() / "broken.json"
        path.write_text("{not json at all")
        with self.assertRaises(Exception):
            jobs.load_session(path)


class TestMachineSetup(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="pistorm-machine-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        #  A stand-in PiMiga layout.
        disks = self.folder / "pimiga" / "disks"
        for drive in ("System", "Games", "Demos", "Work"):
            (disks / drive).mkdir(parents=True)
        (disks / "System" / "C").mkdir()
        (disks / "System" / "C" / "WHDLoad").write_bytes(b"whdload")
        (disks / "System" / "Expansion" / "WHDLoad").mkdir(parents=True)
        (disks / "Games" / "WHDLOAD" / "AGA").mkdir(parents=True)
        (disks / "Games" / "WHDLOAD" / "OCS").mkdir(parents=True)
        self.pimiga = self.folder / "pimiga"
        (self.folder / "adfs").mkdir()

    def detected(self) -> presets.Detected:
        """Stands in for a machine that has a Workbench disk set to hand."""
        return presets.Detected(adf_folder=str(self.folder / "adfs"),
                                adf_version="3.1", adf_complete=True,
                                adf_summary="Workbench, Extras")

    def setup_for(self, key: str, display=Display.NATIVE, size=64 * GIB):
        return presets.machine_setup(
            machines.MACHINES_BY_KEY[key], display, str(self.folder / "c.img"),
            False, size, self.detected(), pimiga_folder=str(self.pimiga))

    def test_finds_the_pimiga_drives(self):
        self.assertIsNotNone(presets.pimiga_disks(self.pimiga))
        self.assertIsNone(presets.pimiga_disks(self.folder / "nowhere"))

    def test_layout_is_valid_and_has_one_flexible_partition(self):
        config = self.setup_for("a500")
        self.assertEqual(config.validate(), [])
        flexible = [p for p in config.amiga_partitions if p.size is None]
        self.assertEqual(len(flexible), 1,
                         "exactly one partition may take the remaining space")

    def test_every_drive_comes_from_pimiga(self):
        """A PiMiga source supplies the whole card, system included."""
        config = self.setup_for("a500")
        sources = {p.name: Path(p.content_folder).name
                   for p in config.amiga_partitions if p.content_folder}
        #  PiMiga's own Amiberry configuration assigns DH1:Demos and
        #  DH2:Games; copying "the PiMiga setup" means keeping that order.
        self.assertEqual(sources, {"DH0": "System", "DH1": "Demos",
                                   "DH2": "Games", "DH3": "Work"})

    def test_volume_labels_match_pimigas_own(self):
        """Workbench shows the volume name, so the drives keep theirs."""
        config = self.setup_for("a500")
        labels = {p.name: p.volume_name for p in config.amiga_partitions}
        self.assertEqual(labels, {"DH0": "System", "DH1": "Demos",
                                  "DH2": "Games", "DH3": "Work"})

    def test_filled_partitions_are_not_described_as_needing_formatting(self):
        """They are populated by the build, so saying otherwise is wrong."""
        config = self.setup_for("a500")
        text = presets.describe(config, self.detected())
        self.assertNotIn("format it on the Amiga", text)
        self.assertIn("copied from Games", text)

    def test_whdload_is_overlaid_onto_a_floppy_install(self):
        """A Workbench built from floppies has no WHDLoad, so it is added."""
        config = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected(),
            pimiga_folder=str(self.pimiga), system_source="adf")
        destinations = [dest for _src, dest in config.amiga_partitions[0].overlays]
        self.assertIn("C", destinations)
        self.assertIn("Expansion/WHDLoad", destinations)

    def test_the_board_follows_the_model(self):
        self.assertEqual(self.setup_for("a500").variant, "pistorm")
        self.assertEqual(self.setup_for("a1200").variant, "pistorm32lite")

    def test_aga_content_is_excluded_only_where_it_cannot_run(self):
        a500 = {p.name: p.exclude for p in self.setup_for("a500").amiga_partitions}
        self.assertIn("WHDLOAD/AGA", a500["DH1"])
        a1200 = {p.name: p.exclude for p in self.setup_for("a1200").amiga_partitions}
        self.assertEqual(a1200["DH1"], [])

    def test_a_small_card_is_refused_rather_than_silently_squeezed(self):
        with self.assertRaises(ValueError):
            self.setup_for("a500", size=300 * MIB)

    def test_a_pimiga_source_is_used_whatever_the_display(self):
        """Choosing PiMiga means using PiMiga: the graphics setup is adapted to
        the machine, not the whole operating system swapped for another."""
        for key, display in (("a500", Display.NATIVE),
                             ("a1200", Display.RTG_HDMI)):
            config = self.setup_for(key, display)
            self.assertEqual(config.system_source, "pimiga", key)
            self.assertFalse(config.install_amigaos, key)
            self.assertTrue(
                config.amiga_partitions[0].content_folder.endswith("System"), key)

    def test_the_display_decides_how_graphics_are_adapted(self):
        self.assertFalse(self.setup_for("a500", Display.NATIVE).rtg_display)
        self.assertTrue(self.setup_for("a1200", Display.RTG_HDMI).rtg_display)
        self.assertTrue(self.setup_for("a500", Display.FRAMETHROWER).rtg_display)

    def test_no_source_at_all_is_surfaced(self):
        """Neither source available must not quietly produce an empty drive."""
        config = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, presets.Detected())
        self.assertFalse(config.install_amigaos)
        text = presets.describe_machine_setup(
            config, machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            presets.Detected())
        self.assertIn("Nothing will be installed onto the boot drive", text)

    def test_without_a_pimiga_source_the_floppies_are_used(self):
        config = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected())
        self.assertEqual(config.system_source, "adf")
        self.assertTrue(config.install_amigaos)

    def test_the_choice_can_be_forced_either_way(self):
        forced = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected(),
            pimiga_folder=str(self.pimiga), system_source="pimiga")
        self.assertEqual(forced.system_source, "pimiga")
        back = presets.machine_setup(
            machines.MACHINES_BY_KEY["a1200"], Display.RTG_HDMI,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected(),
            pimiga_folder=str(self.pimiga), system_source="adf")
        self.assertEqual(back.system_source, "adf")

    def test_whdload_is_only_overlaid_when_installing_from_floppies(self):
        """PiMiga's own system already has WHDLoad; a fresh Workbench does not."""
        from_floppies = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected(),
            pimiga_folder=str(self.pimiga), system_source="adf")
        self.assertTrue(from_floppies.amiga_partitions[0].overlays)
        self.assertFalse(self.setup_for("a1200", Display.RTG_HDMI)
                         .amiga_partitions[0].overlays)

    def test_description_mentions_the_machine_and_display(self):
        config = self.setup_for("a500")
        text = presets.describe_machine_setup(
            config, machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            self.detected())
        self.assertIn("Amiga 500", text)
        self.assertIn("PiStorm (classic)", text)
        self.assertIn("chip_slowdown", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
