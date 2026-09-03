"""Tests for target machine profiles and the hardware-driven setup."""
import json
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
        self.assertEqual(trapdoor, {"a500", "a500ecs", "a500plus"})


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
        back, restored, _reduced = jobs.load_session(path)
        self.assertEqual(back, config)
        self.assertEqual(restored, choices)

    def test_a_file_without_choices_still_loads(self):
        """Settings saved before the choices were recorded must not break."""
        config = builder.BuildConfig(target="/tmp/card.img")
        path = self.scratch() / "old.json"
        jobs.save(config, path)
        back, choices, _reduced = jobs.load_session(path)
        self.assertEqual(back, config)
        self.assertEqual(choices, {})

    def test_a_session_from_before_the_fix_drops_its_partitions(self):
        """Version 1 recorded partitions whose contents had been lost.

        Restoring them would rebuild the card empty with nothing to say so, so
        the layout is discarded and worked out again instead.
        """
        path = self.scratch() / "old.json"
        path.write_text(json.dumps({
            "version": 1,
            "config": {
                "mode": "fresh", "target": "/tmp/card.img",
                "amiga_partitions": [
                    {"name": "DH0", "size": None, "dostype": "PFS3",
                     "bootable": True, "boot_priority": 0,
                     "content_folder": "", "content_hdf": "",
                     "content_hdf_partition": "", "volume_name": "",
                     "overlays": [], "exclude": []}],
            },
            "interface": {"machine": "a500"},
        }))
        config, choices, reduced = jobs.load_session(path)
        self.assertTrue(reduced, "the caller must be told it was reduced")
        self.assertEqual(choices["machine"], "a500",
                         "the rest of the setup is still worth keeping")
        #  Falls back to the default layout rather than an empty one.
        self.assertTrue(config.amiga_partitions)
        self.assertEqual(config.validate(), [])

    def test_a_current_session_keeps_its_partitions(self):
        config = builder.BuildConfig(
            target="/tmp/card.img",
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH2", None, "PFS3", False, -128,
                content_folder="/x/Games", volume_name="Games")])
        path = self.scratch() / "new.json"
        jobs.save_session(config, {}, path)
        back, _choices, reduced = jobs.load_session(path)
        self.assertFalse(reduced)
        self.assertEqual(back.amiga_partitions[0].volume_name, "Games")
        self.assertEqual(back.amiga_partitions[0].content_folder, "/x/Games")

    def test_the_system_source_is_stored_by_name(self):
        """Storing the combo's position breaks when an option is inserted.

        A saved session that meant "install from floppies" would silently come
        back meaning something else entirely.
        """
        from pistorm_imager.ui import window as ui  # noqa: PLC0415
        self.assertIn("image", ui.SYSTEM_SOURCES)
        config = builder.BuildConfig(target="/tmp/card.img")
        path = self.scratch() / "s.json"
        jobs.save_session(config, {"system_source": "adf"}, path)
        _cfg, choices, _reduced = jobs.load_session(path)
        self.assertEqual(choices["system_source"], "adf")

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
        (disks / "System" / "S").mkdir()
        (disks / "System" / "C" / "WHDLoad").write_bytes(b"whdload")
        (disks / "System" / "C" / "lha").write_bytes(b"lha")
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

    def test_whdload_is_chosen_for_a_floppy_install(self):
        """A Workbench built from floppies has no WHDLoad, so it is added.

        The files themselves are fetched from Aminet while the card is being
        built, where there is a progress report to hang the download off, so
        what the configuration carries is the choice.
        """
        config = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected(),
            pimiga_folder=str(self.pimiga), system_source="adf")
        self.assertIn("whdload", config.package_keys)
        self.assertIn("lha", config.package_keys)

    def test_the_work_drive_switch_is_honoured(self):
        """It was passed nowhere, so turning it off changed nothing."""
        with_work = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected(),
            work_partition=True)
        without = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected(),
            work_partition=False)
        self.assertEqual(len(with_work.amiga_partitions), 2)
        self.assertEqual(len(without.amiga_partitions), 1)

    def test_a_source_still_brings_its_own_drives(self):
        """The switch is about a drive we would add, not the source's own."""
        config = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected(),
            pimiga_folder=str(self.pimiga), work_partition=False)
        self.assertEqual(len(config.amiga_partitions), 4)

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

    def test_software_is_only_chosen_when_installing_from_floppies(self):
        """A ready-made system brings its own; a fresh Workbench does not."""
        from_floppies = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(self.folder / "c.img"), False, 64 * GIB, self.detected(),
            pimiga_folder=str(self.pimiga), system_source="adf")
        self.assertTrue(from_floppies.package_keys)
        self.assertFalse(self.setup_for("a1200", Display.RTG_HDMI)
                         .package_keys)

    def test_description_mentions_the_machine_and_display(self):
        config = self.setup_for("a500")
        text = presets.describe_machine_setup(
            config, machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            self.detected())
        self.assertIn("Amiga 500", text)
        self.assertIn("PiStorm (classic)", text)
        self.assertIn("chip_slowdown", text)




class TestOptionalSoftware(unittest.TestCase):
    """A Workbench from floppies has no archiver, installer or WHDLoad.

    None of that is shipped here, so each piece is fetched from whoever
    publishes it. It used to be copied out of a system the user already had,
    which meant a card was built from whatever that installation happened to
    hold, at whatever age it happened to be.
    """

    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-pkg-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder

    def test_everything_offered_names_where_it_comes_from(self):
        from pistorm_imager.core import packages
        for package in packages.CATALOGUE:
            self.assertIsNotNone(
                package.download,
                f"{package.key} has no source, so ticking it would do nothing")

    def test_rtg_only_software_is_withheld_without_rtg(self):
        from pistorm_imager.core import packages
        self.assertEqual(
            packages.overlays_for(["picasso96"], rtg=False,
                                  allow_download=False), [])

    def test_nothing_is_fetched_when_downloads_are_not_allowed(self):
        """The configuration stage resolves no files; the build fetches them."""
        from pistorm_imager.core import packages
        self.assertEqual(
            packages.overlays_for(["whdload", "lha"], rtg=False,
                                  allow_download=False), [])

    def test_the_setup_adds_them_to_a_floppy_install(self):
        folder = self.scratch()
        (folder / "adfs").mkdir()
        config = presets.machine_setup(
            machines.MACHINES_BY_KEY["a500"], Display.NATIVE,
            str(folder / "c.img"), False, 32 * 10 ** 9,
            presets.Detected(adf_folder=str(folder / "adfs"), adf_version="3.1",
                             adf_complete=True),
            system_source="adf", package_keys=["whdload", "lha"])
        self.assertEqual(config.package_keys, ["whdload", "lha"])


class TestPackageFit(unittest.TestCase):
    """Which optional software suits which machine and screen."""

    def setUp(self):
        from pistorm_imager.core import packages
        self.packages = packages
        self.a500 = machines.MACHINES_BY_KEY["a500"]
        self.a1200 = next(m for m in machines.MACHINES if m.aga)

    def donor(self, *present: str) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-donor-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        system = folder / "System"
        (system / "C").mkdir(parents=True)
        for item in present:
            path = system / item
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        return system

    def test_every_entry_is_obtainable_somehow(self):
        """A package nobody can supply would only ever be a dead switch."""
        for package in self.packages.CATALOGUE:
            self.assertTrue(package.download, package.key)

    def test_a_self_installing_download_says_where_it_lands(self):
        """A package placed whole has to name the drawer it goes into.

        "Placed whole" is the archive going somewhere as it is, which is what
        happens when nothing says where its parts belong - so a package that
        places files by `rename`, or writes its own, is not one of these. The
        condition used to be `items` alone, and a package with only a rename
        fell through to being placed whole at `stage`, which for such a
        package is "" - the volume root.
        """
        for package in self.packages.CATALOGUE:
            download = package.download
            if download is None or download.merge:
                continue
            if not (download.items or download.rename or download.write):
                self.assertTrue(download.stage, package.key)
                self.assertTrue(package.note, package.key)

    def test_fblit_is_for_the_amigas_own_screen(self):
        fblit = self.packages.CATALOGUE_BY_KEY["fblit"]
        self.assertTrue(fblit.suits(machines.Chipset.OCS, Display.NATIVE))
        self.assertTrue(fblit.suits(machines.Chipset.OCS, Display.BOTH))
        self.assertFalse(fblit.suits(machines.Chipset.AGA, Display.RTG_HDMI))

    def test_picasso96_is_for_an_rtg_screen(self):
        p96 = self.packages.CATALOGUE_BY_KEY["picasso96"]
        self.assertFalse(p96.suits(machines.Chipset.AGA, Display.NATIVE))
        self.assertTrue(p96.suits(machines.Chipset.OCS, Display.RTG_HDMI))

    def test_scalos_wants_aga_or_an_rtg_screen(self):
        scalos = self.packages.CATALOGUE_BY_KEY["scalos"]
        self.assertFalse(scalos.suits(machines.Chipset.OCS, Display.NATIVE))
        self.assertTrue(scalos.suits(machines.Chipset.AGA, Display.NATIVE))
        self.assertTrue(scalos.suits(machines.Chipset.OCS, Display.RTG_HDMI))

    def test_the_suggestion_for_an_ocs_a500_on_its_own_screen(self):
        chosen = self.packages.suggested(self.a500, Display.NATIVE)
        for key in ("whdload", "lha", "installer", "iconlib",
                    "fblit", "ftext", "fullpalette"):
            self.assertIn(key, chosen)
        self.assertNotIn("picasso96", chosen)
        self.assertNotIn("scalos", chosen)

    def test_the_suggestion_for_an_aga_machine_on_hdmi(self):
        chosen = self.packages.suggested(self.a1200, Display.RTG_HDMI)
        self.assertIn("picasso96", chosen)
        #  Nothing that only helps the Amiga's own chipset draw a screen.
        self.assertNotIn("fblit", chosen)

    def test_a_suggestion_only_names_software_that_can_be_had(self):
        """Every suggestion has to be something this can actually install."""
        for machine, display in ((self.a1200, Display.RTG_HDMI),
                                 (self.a500, Display.NATIVE)):
            for key in self.packages.suggested(machine, display,
                                               networking=True):
                self.assertIsNotNone(
                    self.packages.CATALOGUE_BY_KEY[key].download,
                    f"{key} is suggested and cannot be obtained")

    def test_networking_is_only_suggested_when_it_is_wanted(self):
        without = self.packages.suggested(self.a500, Display.NATIVE)
        self.assertNotIn("netsurf", without)
        with_net = self.packages.suggested(self.a500, Display.NATIVE,
                                           networking=True)
        self.assertIn("netsurf", with_net)

    def test_every_suggestion_is_a_real_key(self):
        for machine in machines.MACHINES:
            for display in Display:
                for key in self.packages.suggested(machine, display,
                                                   networking=True):
                    self.assertIn(key, self.packages.CATALOGUE_BY_KEY)


class TestBothOutputs(unittest.TestCase):
    """A PiStorm target can use HDMI RTG and the Amiga's own video at once."""

    def test_both_counts_as_rtg_and_as_native(self):
        both = machines.Display.BOTH
        self.assertTrue(both.uses_rtg)
        self.assertTrue(both.uses_native)
        self.assertTrue(both.has_choice_of_screen)

    def test_a_single_output_leaves_nothing_to_choose(self):
        for display in (machines.Display.NATIVE, machines.Display.RTG_HDMI):
            self.assertFalse(display.has_choice_of_screen, display)

    def test_the_screen_choice_is_only_honoured_where_there_is_one(self):
        rtg = machines.Display.RTG_HDMI
        native = machines.Display.NATIVE
        both = machines.Display.BOTH
        self.assertTrue(machines.workbench_on_rtg(rtg, prefer_rtg=False),
                        "with no native output there is nowhere else to go")
        self.assertFalse(machines.workbench_on_rtg(native, prefer_rtg=True),
                         "with no RTG there is nowhere else to go")
        self.assertTrue(machines.workbench_on_rtg(both, prefer_rtg=True))
        self.assertFalse(machines.workbench_on_rtg(both, prefer_rtg=False))

    def test_hdmi_is_still_configured_for_both(self):
        machine = machines.MACHINES_BY_KEY["a500"]
        options = machines.boot_options(machine, machines.Display.BOTH,
                                        hdmi=(1, 16))
        self.assertEqual((options.hdmi_group, options.hdmi_mode), (1, 16))
        self.assertEqual(options.vc4_mem, 64)
        self.assertFalse(options.unicam,
                         "unicam is the Framethrower's capture, not this")

    def test_a_build_carries_both_outputs(self):
        machine = machines.MACHINES_BY_KEY["a500"]
        config = presets.machine_setup(
            machine, machines.Display.BOTH, "/tmp/card.img", False,
            32 * GIB, presets.Detected(), system_source="none",
            prefer_rtg_screen=False)
        self.assertTrue(config.rtg_display)
        self.assertTrue(config.native_display)
        self.assertFalse(config.workbench_on_rtg)
        text = presets.describe_machine_setup(config, machine,
                                              machines.Display.BOTH,
                                              presets.Detected())
        self.assertIn("Workbench opens on", text)


class EcsUpgrades(unittest.TestCase):
    """A rev 6A A500 with a Super Denise is ECS, not OCS.

    Common enough an upgrade that offering only a plain OCS A500 gets the
    chipset wrong for a real machine - and the chipset decides which game
    collections are worth copying and which screen modes exist.
    """

    def test_an_ecs_a500_can_be_chosen(self):
        by_key = {m.key: m for m in machines.MACHINES}
        self.assertIn("a500ecs", by_key)
        self.assertIs(by_key["a500ecs"].chipset, machines.Chipset.ECS)

    def test_it_is_still_a_classic_pistorm_with_trapdoor_ram(self):
        machine = next(m for m in machines.MACHINES if m.key == "a500ecs")
        self.assertEqual(machine.board, "pistorm")
        self.assertTrue(machine.trapdoor_ram)

    def test_the_plain_a500_points_at_it(self):
        #  Otherwise someone with the upgrade has no way of knowing.
        machine = next(m for m in machines.MACHINES if m.key == "a500")
        self.assertIn("Super Denise", machine.notes)

    def test_ecs_still_gets_the_chipset_slowdown(self):
        #  ECS software busy-waits on the chipset just as OCS does.
        machine = next(m for m in machines.MACHINES if m.key == "a500ecs")
        options = machines.boot_options(machine, machines.Display.NATIVE)
        self.assertTrue(options.chip_slowdown)
        self.assertTrue(options.enable_slow_ram)


class TheTrapdoorRamHasToBeMappedBeforeItCanBeMoved(unittest.TestCase):
    """``move_slow_to_chip`` on its own moves nothing.

    The RAM at 0xC00000 has to be mapped first, which is what
    ``enable_c0_slow`` and its neighbours do. Sent without them the option is
    inert, and a machine told to give Workbench a megabyte of chip RAM comes
    up with 512K - which is what a real card did.
    """

    def cmdline(self, key: str, trapdoor: bool) -> str:
        machine = machines.MACHINES_BY_KEY[key]
        return machines.boot_options(machine, machines.Display.NATIVE,
                                     trapdoor_to_chip=trapdoor).cmdline()

    def test_the_two_travel_together(self):
        for key in ("a500", "a500ecs"):
            with self.subTest(key):
                text = self.cmdline(key, True)
                self.assertIn("move_slow_to_chip", text)
                self.assertIn("enable_c0_slow", text)

    def test_the_ranges_are_mapped_even_without_the_trapdoor_choice(self):
        #  Enabling the ranges is right for any OCS/ECS machine; moving them
        #  into chip RAM is the separate choice.
        text = self.cmdline("a500ecs", False)
        self.assertIn("enable_c0_slow", text)
        self.assertNotIn("move_slow_to_chip", text)

    def test_an_aga_machine_gets_neither(self):
        text = self.cmdline("a1200", True)
        self.assertNotIn("enable_c0_slow", text)

    def test_the_option_names_are_the_ones_emu68_knows(self):
        #  Checked against the strings in the Emu68 kernel binary rather than
        #  from memory: a switch spelled wrongly does nothing at all, quietly.
        text = self.cmdline("a500ecs", True)
        for word in ("enable_c0_slow", "enable_c8_slow", "enable_d0_slow",
                     "move_slow_to_chip"):
            self.assertIn(word, text)


class EveryOptionTheMachineDecidesReachesTheCard(unittest.TestCase):
    """gather() builds the boot options, and it built them from widgets alone.

    Two settings have no widget - the machine or the display decides them -
    and both were therefore left at their dataclass default on every card
    written from the pages: the slow RAM mapping, without which
    ``move_slow_to_chip`` moves nothing and a machine told to give Workbench a
    megabyte of chip RAM comes up with 512K, and the framethrower overlay,
    without which choosing that display drives nothing.

    A save/load round trip cannot catch this - a field never set at all is
    consistently wrong in both directions - so the invariant is checked
    directly: anything ``machines.boot_options()`` can decide must either be
    passed by ``gather()`` or be owned by a widget it reads.
    """

    def gather_keywords(self) -> set:
        """The BootOptions keywords gather() actually passes."""
        import ast                                             # noqa: PLC0415
        source = (Path(__file__).resolve().parent.parent
                  / "pistorm_imager" / "ui" / "window.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == "gather":
                for call in ast.walk(node):
                    if isinstance(call, ast.Call):
                        func = call.func
                        name = (func.attr if isinstance(func, ast.Attribute)
                                else getattr(func, "id", ""))
                        if name == "BootOptions":
                            return {kw.arg for kw in call.keywords if kw.arg}
        self.fail("gather() no longer builds a BootOptions")
        return set()

    def test_gather_passes_everything_the_machine_can_decide(self):
        import dataclasses                                     # noqa: PLC0415
        from pistorm_imager.core import bootcfg                # noqa: PLC0415
        passed = self.gather_keywords()
        default = bootcfg.BootOptions()
        dropped = {}
        for machine in machines.MACHINES:
            for display in machines.Display:
                for trapdoor in (False, True):
                    decided = machines.boot_options(
                        machine, display, trapdoor_to_chip=trapdoor)
                    for field in dataclasses.fields(decided):
                        value = getattr(decided, field.name)
                        if field.name in passed:
                            continue
                        if value != getattr(default, field.name):
                            dropped.setdefault(field.name, set()).add(
                                f"{machine.key}/{display.name}")
        self.assertEqual(
            dropped, {},
            "gather() drops settings the machine decides, so a card written "
            f"from the pages goes out without them: {dropped}")


class TheEmulatorIsToldWhatTheCardWasBuiltFor(unittest.TestCase):
    """Testing a card means describing the Amiga it is going into.

    That description already exists - the model, the chipset, the board and
    the trapdoor choice drive the build itself - and writing it a second time
    by hand is how the two came apart: the harness used through one long
    bisection emulated an A1200 (AGA) with an FPU at accuracy = 0, while the
    card was built for an ECS A500 on a PiStorm that has no FPU.
    """

    def config(self, key: str, **kwargs) -> str:
        from pistorm_imager.core import emulate                # noqa: PLC0415
        return emulate.fsuae_config(machines.MACHINES_BY_KEY[key],
                                    "/tmp/card.hdf", "/tmp/kick.rom", **kwargs)

    def setting(self, text: str, name: str) -> str:
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            if line.split("=")[0].strip() == name:
                return line.split("=", 1)[1].strip()
        return ""

    def test_the_chipset_follows_the_machine(self):
        self.assertEqual(self.setting(self.config("a500"), "amiga_model"),
                         "A500")
        self.assertEqual(self.setting(self.config("a1200"), "amiga_model"),
                         "A1200")
        #  An ECS A500 is not an OCS one, and emulating it as A500 would get
        #  the chipset wrong for the very material this project converts.
        self.assertNotEqual(self.setting(self.config("a500ecs"),
                                         "amiga_model"), "A500")

    def test_never_the_fast_inexact_cpu(self):
        for machine in machines.MACHINES:
            with self.subTest(machine.key):
                self.assertEqual(
                    self.setting(self.config(machine.key), "accuracy"), "1",
                    "accuracy = 0 makes every WHDLoad game guru, which reads "
                    "like a broken card and is not")

    def test_the_trapdoor_choice_decides_the_chip_ram(self):
        without = self.setting(self.config("a500ecs"), "chip_memory")
        with_it = self.setting(self.config("a500ecs", trapdoor_to_chip=True),
                               "chip_memory")
        self.assertEqual(without, "512")
        self.assertEqual(with_it, "1024")

    def test_a_machine_with_no_trapdoor_is_unaffected(self):
        self.assertEqual(
            self.setting(self.config("a600", trapdoor_to_chip=True),
                         "chip_memory"),
            self.setting(self.config("a600"), "chip_memory"))

    def test_every_machine_has_a_model_to_stand_in_for_it(self):
        from pistorm_imager.core import emulate                # noqa: PLC0415
        for machine in machines.MACHINES:
            with self.subTest(machine.key):
                self.assertTrue(emulate.fsuae_model(machine))


if __name__ == "__main__":
    unittest.main(verbosity=2)
