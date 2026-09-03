"""Tests for the automatic PiStorm compatibility fixes."""
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import amigainfo, builder, compat, mbr, pfs3, rdb  # noqa: E402
from pistorm_imager.core.util import MIB, Progress  # noqa: E402

QUIET = Progress()

REAL_ICON = Path("/media/pclarke/rootfs/home/pi/pimiga/disks/System/Devs/"
                 "Monitors/uaegfx.info")


def make_icon(tooltypes: list[str]) -> bytes:
    """A minimal but structurally valid .info file carrying tool types."""
    data = bytearray(78)
    struct.pack_into(">HH", data, 0, amigainfo.MAGIC, 1)
    struct.pack_into(">I", data, 54, 1)          # do_ToolTypes present
    block = bytearray(struct.pack(">I", (len(tooltypes) + 1) * 4))
    for entry in tooltypes:
        raw = entry.encode("latin-1") + b"\0"
        block += struct.pack(">I", len(raw)) + raw
    return bytes(data + block)


class _Scratch(unittest.TestCase):
    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-compat-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder


class TestIconToolTypes(unittest.TestCase):
    def test_round_trip(self):
        icon = make_icon(["BOARDTYPE=uaegfx", "IGNOREMASK=Yes"])
        self.assertEqual(amigainfo.read_tooltypes(icon),
                         ["BOARDTYPE=uaegfx", "IGNOREMASK=Yes"])

    def test_set_replaces_only_the_named_key(self):
        icon = make_icon(["BOARDTYPE=uaegfx", "IGNOREMASK=Yes"])
        updated = amigainfo.set_tooltype(icon, "BOARDTYPE", "VideoCore")
        self.assertEqual(amigainfo.read_tooltypes(updated),
                         ["BOARDTYPE=VideoCore", "IGNOREMASK=Yes"])

    def test_set_adds_a_missing_key(self):
        icon = make_icon(["IGNOREMASK=Yes"])
        updated = amigainfo.set_tooltype(icon, "BOARDTYPE", "VideoCore")
        self.assertIn("BOARDTYPE=VideoCore", amigainfo.read_tooltypes(updated))

    def test_rejects_a_non_icon(self):
        with self.assertRaises(amigainfo.InfoError):
            amigainfo.read_tooltypes(b"not an icon at all")

    @unittest.skipUnless(REAL_ICON.is_file(), "no real Picasso96 icon available")
    def test_real_picasso96_icon(self):
        try:
            data = REAL_ICON.read_bytes()
        except OSError as error:
            #  The source is a loop-mounted disk image and does occasionally
            #  throw a transient read error; that is not a failure of this code.
            self.skipTest(f"could not read the sample icon: {error}")
        entries = amigainfo.read_tooltypes(data)
        self.assertTrue(any(e.startswith("BOARDTYPE=") for e in entries))
        updated = amigainfo.set_tooltype(data, "BOARDTYPE", "VideoCore")
        self.assertEqual(amigainfo.read_tooltypes(updated)[0],
                         "BOARDTYPE=VideoCore")
        #  Everything else must survive untouched.
        self.assertEqual(amigainfo.read_tooltypes(updated)[1:], entries[1:])


class TestStartupCleaning(unittest.TestCase):
    def fixer(self) -> compat.Compatibility:
        return compat.Compatibility(QUIET, enabled=True)

    def test_comments_out_emulator_commands(self):
        script = (b"; startup\n"
                  b"SetPatch QUIET\n"
                  b"uae-configuration cachesize 16384\n"
                  b"LoadWB\n")
        out = self.fixer().offer("S/Startup-Sequence", script).decode()
        self.assertIn("; [PiStorm] uae-configuration", out)
        self.assertIn("SetPatch QUIET", out)
        self.assertIn("LoadWB", out)
        self.assertNotIn("; [PiStorm] SetPatch", out)

    def test_leaves_ordinary_scripts_alone(self):
        script = b"SetPatch QUIET\nLoadWB\n"
        self.assertEqual(self.fixer().offer("S/Startup-Sequence", script), script)

    def test_disabled_fixer_changes_nothing(self):
        fixer = compat.Compatibility(QUIET, enabled=False)
        script = b"uae-configuration cachesize 1\n"
        self.assertEqual(fixer.offer("S/Startup-Sequence", script), script)
        self.assertFalse(fixer.skip("Libs/Picasso96/uaegfx.card"))


class TestDriverSwap(unittest.TestCase):
    def test_emulator_rtg_driver_is_dropped(self):
        fixer = compat.Compatibility(QUIET, enabled=True)
        fixer.offer("Libs/Picasso96/uaegfx.card", b"emulator driver")
        self.assertTrue(fixer.skip("Libs/Picasso96/uaegfx.card"))

    def test_monitor_and_icon_are_captured_for_replacement(self):
        fixer = compat.Compatibility(QUIET, enabled=True)
        fixer.offer("Devs/Monitors/uaegfx", b"monitor loader")
        self.assertTrue(fixer.skip("Devs/Monitors/uaegfx"))
        self.assertEqual(fixer.monitor_file, b"monitor loader")
        icon = make_icon(["BOARDTYPE=uaegfx"])
        fixer.offer("Devs/Monitors/uaegfx.info", icon)
        self.assertTrue(fixer.skip("Devs/Monitors/uaegfx.info"))
        self.assertEqual(fixer.monitor_icon, icon)

    def test_unrelated_files_are_kept(self):
        fixer = compat.Compatibility(QUIET, enabled=True)
        for path in ("Libs/Picasso96/rtg.library", "C/LoadWB",
                     "Devs/Monitors/PAL"):
            fixer.offer(path, b"x")
            self.assertFalse(fixer.skip(path), path)


class TestDisplayAdaptation(unittest.TestCase):
    """With no RTG the emulator's graphics driver is removed, not replaced."""

    def native(self) -> compat.Compatibility:
        return compat.Compatibility(QUIET, enabled=True, rtg=False)

    def test_rtg_driver_is_removed_on_a_native_machine(self):
        fixer = self.native()
        fixer.offer("Devs/Monitors/uaegfx", b"monitor")
        self.assertTrue(fixer.skip("Devs/Monitors/uaegfx"))
        #  Nothing is kept for replacement, because nothing will replace it.
        self.assertIsNone(fixer.monitor_file)

    def test_saved_rtg_screen_mode_is_dropped_on_a_native_machine(self):
        fixer = self.native()
        path = "Prefs/Env-Archive/Sys/ScreenMode.prefs"
        fixer.offer(path, b"binary prefs")
        self.assertTrue(fixer.skip(path),
                        "a saved RTG screen mode would strand Workbench")

    def test_saved_screen_mode_is_kept_when_there_is_rtg(self):
        fixer = compat.Compatibility(QUIET, enabled=True, rtg=True)
        path = "Prefs/Env-Archive/Sys/ScreenMode.prefs"
        fixer.offer(path, b"binary prefs")
        self.assertFalse(fixer.skip(path))

    def test_no_rtg_driver_is_installed_on_a_native_machine(self):
        fixer = self.native()
        fixer.offer("Libs/Picasso96/rtg.library", b"p96")

        class FakeVolume:
            def __init__(self):
                self.written = []
            root = 0

            def makedirs(self, path):
                return 1

            def write_file(self, parent, name, data, **kwargs):
                self.written.append(name)

        volume = FakeVolume()
        fixer.finish(volume, QUIET)
        self.assertEqual(volume.written, [],
                         "nothing should be installed for a display that has "
                         "no RTG")


class FakeVolume:
    """Just enough of a filled volume to see what the fixer adds to it."""

    root = 0

    def __init__(self):
        self.written: list[tuple[str, str]] = []
        self._dirs: dict[int, str] = {0: ""}

    def makedirs(self, path):
        handle = len(self._dirs) + 1
        self._dirs[handle] = path
        return handle

    def write_file(self, parent, name, data, **kwargs):
        self.written.append((self._dirs.get(parent, "?"), name))


class TestBothDisplays(unittest.TestCase):
    """A machine can have RTG on the Pi's HDMI *and* its own video port in use.

    That is neither of the two cases the fixer used to know about: the RTG
    driver still has to be installed, but Workbench may belong on the Amiga's
    own output, and native screen modes have to be selectable at all.
    """

    def both(self, on_rtg: bool) -> compat.Compatibility:
        return compat.Compatibility(QUIET, enabled=True, rtg=True,
                                    native=True, workbench_on_rtg=on_rtg)

    def test_workbench_on_rtg_keeps_the_saved_screen_mode(self):
        fixer = self.both(on_rtg=True)
        path = "Prefs/Env-Archive/Sys/ScreenMode.prefs"
        fixer.offer(path, b"binary prefs")
        self.assertFalse(fixer.skip(path))

    def test_workbench_on_the_amiga_drops_the_saved_rtg_screen_mode(self):
        fixer = self.both(on_rtg=False)
        path = "Prefs/Env-Archive/Sys/ScreenMode.prefs"
        fixer.offer(path, b"binary prefs")
        self.assertTrue(fixer.skip(path),
                        "a saved RTG mode would open Workbench on the HDMI "
                        "screen, not the Amiga's own output")

    def test_the_rtg_driver_is_still_replaced_not_removed(self):
        fixer = self.both(on_rtg=False)
        fixer.offer("Devs/Monitors/uaegfx", b"monitor")
        self.assertTrue(fixer.skip("Devs/Monitors/uaegfx"))
        self.assertEqual(fixer.monitor_file, b"monitor",
                         "RTG is still in use, so the monitor is kept to be "
                         "retargeted rather than thrown away")

    def test_a_native_monitor_is_installed_from_storage(self):
        fixer = self.both(on_rtg=True)
        fixer.offer("Devs/Monitors/uaegfx", b"monitor")
        fixer.offer("Storage/Monitors/PAL", b"pal driver")
        fixer.offer("Storage/Monitors/PAL.info", b"pal icon")
        volume = FakeVolume()
        fixer.finish(volume, QUIET)
        self.assertIn(("Devs/Monitors", "PAL"), volume.written,
                      "with the Amiga's video port in use there must be a "
                      "native screen mode to choose")
        self.assertIn(("Devs/Monitors", "PAL.info"), volume.written)

    def test_an_installed_native_monitor_is_left_alone(self):
        fixer = self.both(on_rtg=True)
        fixer.offer("Devs/Monitors/PAL", b"already installed")
        fixer.offer("Storage/Monitors/PAL", b"spare copy")
        volume = FakeVolume()
        fixer.finish(volume, QUIET)
        self.assertNotIn(("Devs/Monitors", "PAL"), volume.written,
                         "a driver already in place is not written over")

    def test_nothing_native_is_installed_for_an_rtg_only_display(self):
        fixer = compat.Compatibility(QUIET, enabled=True, rtg=True,
                                     native=False)
        fixer.offer("Storage/Monitors/PAL", b"pal driver")
        volume = FakeVolume()
        fixer.finish(volume, QUIET)
        self.assertEqual(volume.written, [],
                         "nobody is looking at the Amiga's video port")

    def test_the_display_can_be_switched_on_the_amiga(self):
        """Which monitor is on today is not a property of the card."""
        fixer = self.both(on_rtg=True)
        fixer.offer("Devs/Monitors/PAL", b"native")
        volume = FakeVolume()
        fixer.finish(volume, QUIET)
        self.assertIn(("S", "PiStorm-Use-HDMI"), volume.written)
        self.assertIn(("S", "PiStorm-Use-Amiga-Video"), volume.written)

    def test_a_dropped_rtg_screen_mode_is_kept_for_switching_back(self):
        fixer = self.both(on_rtg=False)
        path = "Prefs/Env-Archive/Sys/ScreenMode.prefs"
        fixer.offer(path, b"the saved RTG mode")
        self.assertTrue(fixer.skip(path))
        volume = FakeVolume()
        fixer.finish(volume, QUIET)
        self.assertIn((compat.SWITCH_STORE, compat.SWITCH_PREFS),
                      volume.written,
                      "the mode is stashed, not thrown away, so switching "
                      "back needs no rebuild")

    def test_no_switcher_where_there_is_only_one_output(self):
        for fixer in (compat.Compatibility(QUIET, rtg=True, native=False),
                      compat.Compatibility(QUIET, rtg=False, native=True)):
            volume = FakeVolume()
            fixer.finish(volume, QUIET)
            self.assertNotIn(("S", "PiStorm-Use-HDMI"), volume.written)

    def test_the_scripts_guard_every_step(self):
        """A bare failing command stops an AmigaDOS script at FAILAT 10."""
        for text in (compat.USE_HDMI_SCRIPT, compat.USE_NATIVE_SCRIPT):
            body = text.format(store=compat.SWITCH_STORE,
                               prefs=compat.SWITCH_PREFS)
            lines = [line.strip() for line in body.splitlines()
                     if line.strip() and not line.strip().startswith(";")]
            for line in lines:
                if line.split()[0].lower() in ("delete", "makedir", "copy"):
                    self.assertTrue(
                        any(l.lower().startswith("if ") for l in lines),
                        f"{line} is not guarded")
            self.assertEqual(
                sum(1 for l in lines if l.lower().startswith("if ")),
                sum(1 for l in lines if l.lower() == "endif"),
                f"unbalanced IF/ENDIF in:\n{body}")

    def test_one_output_ignores_a_stale_preference(self):
        #  Asking for Workbench on the Amiga's output when there is no RTG at
        #  all, and vice versa, must not be honoured.
        rtg_only = compat.Compatibility(QUIET, enabled=True, rtg=True,
                                        native=False, workbench_on_rtg=False)
        self.assertTrue(rtg_only.workbench_on_rtg)
        native_only = compat.Compatibility(QUIET, enabled=True, rtg=False,
                                           native=True, workbench_on_rtg=True)
        self.assertFalse(native_only.workbench_on_rtg)


class TestContentInstall(_Scratch):
    def test_directory_tree_becomes_a_pfs3_partition(self):
        """An emulator-style directory drive becomes a real Amiga partition."""
        folder = self.scratch()
        tree = folder / "System"
        (tree / "S").mkdir(parents=True)
        (tree / "Libs" / "Picasso96").mkdir(parents=True)
        (tree / "Devs" / "Monitors").mkdir(parents=True)
        (tree / "S" / "Startup-Sequence").write_bytes(
            b"SetPatch QUIET\nuae-configuration cachesize 16384\nLoadWB\n")
        (tree / "Libs" / "Picasso96" / "uaegfx.card").write_bytes(b"emu driver")
        (tree / "Libs" / "Picasso96" / "rtg.library").write_bytes(b"p96" * 100)
        (tree / "Devs" / "Monitors" / "uaegfx").write_bytes(b"monitor")
        (tree / "Devs" / "Monitors" / "uaegfx.info").write_bytes(
            make_icon(["BOARDTYPE=uaegfx", "IGNOREMASK=Yes"]))

        target = folder / "card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(target),
            image_size=400 * MIB, boot_size=96 * MIB, install_emu68=False,
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0, content_folder=str(tree))],
        ), QUIET)

        with open(target, "rb") as handle:
            amiga = next(p for p in mbr.read_table(handle)
                         if p.type_id == mbr.TYPE_AMIGA)
            table = rdb.Rdb.read(handle, amiga.start_bytes)
            offset = (amiga.start_bytes
                      + table.partitions[0].start_block(table.geometry) * 512)
            volume = pfs3.Pfs3Volume(handle, offset)

            #  The emulator's driver is gone and nothing was duplicated.
            names = sorted(e.name for e in volume.listdir())
            self.assertEqual(names, ["Devs", "Libs", "S"])
            picasso = volume.find("Libs/Picasso96")
            cards = {e.name for e in volume.listdir(picasso.anode)}
            self.assertNotIn("uaegfx.card", cards)
            self.assertIn("rtg.library", cards)

            #  The startup sequence had its emulator command commented out.
            startup = volume.read_file(volume.find("S/Startup-Sequence")).decode()
            self.assertIn("; [PiStorm] uae-configuration", startup)
            self.assertIn("LoadWB", startup)

    def test_compatibility_can_be_switched_off(self):
        folder = self.scratch()
        tree = folder / "System"
        (tree / "S").mkdir(parents=True)
        (tree / "S" / "Startup-Sequence").write_bytes(b"uae-configuration x\n")
        target = folder / "card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(target),
            image_size=400 * MIB, boot_size=96 * MIB, install_emu68=False,
            fix_compatibility=False,
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0, content_folder=str(tree))],
        ), QUIET)
        with open(target, "rb") as handle:
            amiga = next(p for p in mbr.read_table(handle)
                         if p.type_id == mbr.TYPE_AMIGA)
            table = rdb.Rdb.read(handle, amiga.start_bytes)
            offset = (amiga.start_bytes
                      + table.partitions[0].start_block(table.geometry) * 512)
            volume = pfs3.Pfs3Volume(handle, offset)
            startup = volume.read_file(volume.find("S/Startup-Sequence")).decode()
        self.assertNotIn("[PiStorm]", startup)

    def _tree_with_unreadable_file(self) -> tuple[Path, Path]:
        folder = self.scratch()
        tree = folder / "System"
        (tree / "Libs").mkdir(parents=True)
        (tree / "Libs" / "good.library").write_bytes(b"fine")
        bad = tree / "Libs" / "rtg.library"
        bad.write_bytes(b"original contents")
        bad.chmod(0o000)                      # simulate an unreadable source
        self.addCleanup(bad.chmod, 0o644)
        return folder, tree

    def test_unreadable_source_stops_the_build(self):
        """A file that cannot be read must never be passed over silently."""
        folder, tree = self._tree_with_unreadable_file()
        config = builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(folder / "card.img"),
            image_size=400 * MIB, boot_size=96 * MIB, install_emu68=False,
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0, content_folder=str(tree))])
        with self.assertRaises(RuntimeError) as caught:
            builder.run_build(config, QUIET)
        message = str(caught.exception)
        self.assertIn("rtg.library", message)
        self.assertIn("incomplete", message)

    def test_a_spare_copy_is_used_when_the_source_cannot_be_read(self):
        folder, tree = self._tree_with_unreadable_file()
        spares = folder / "spares"
        spares.mkdir()
        (spares / "rtg.library").write_bytes(b"known good replacement")

        target = folder / "card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(target),
            image_size=400 * MIB, boot_size=96 * MIB, install_emu68=False,
            spare_files_folder=str(spares),
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0, content_folder=str(tree))]), QUIET)

        with open(target, "rb") as handle:
            amiga = next(p for p in mbr.read_table(handle)
                         if p.type_id == mbr.TYPE_AMIGA)
            table = rdb.Rdb.read(handle, amiga.start_bytes)
            offset = (amiga.start_bytes
                      + table.partitions[0].start_block(table.geometry) * 512)
            volume = pfs3.Pfs3Volume(handle, offset)
            entry = volume.find("Libs/rtg.library")
            self.assertIsNotNone(entry, "the substituted file should be present")
            self.assertEqual(volume.read_file(entry), b"known good replacement")
            self.assertEqual(volume.read_file(volume.find("Libs/good.library")),
                             b"fine")

    def test_spares_are_indexed_by_name(self):
        folder = self.scratch()
        (folder / "rtg.library").write_bytes(b"x")
        (folder / "nested").mkdir()
        (folder / "nested" / "other.library").write_bytes(b"y")
        fixer = compat.Compatibility(QUIET)
        self.assertEqual(fixer.add_spares(folder), 2)
        self.assertIsNotNone(fixer.spare_for("Libs/Picasso96/rtg.library"))
        self.assertIsNotNone(fixer.spare_for("Libs/other.library"))
        self.assertIsNone(fixer.spare_for("Libs/absent.library"))

    def test_refuses_content_that_does_not_fit(self):
        folder = self.scratch()
        tree = folder / "Big"
        tree.mkdir()
        (tree / "huge.bin").write_bytes(b"x" * (200 * MIB))
        config = builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(folder / "small.img"),
            image_size=300 * MIB, boot_size=96 * MIB, install_emu68=False,
            amiga_partitions=[builder.AmigaPartitionSpec(
                "DH0", None, "PFS3", True, 0, content_folder=str(tree))])
        with self.assertRaises(RuntimeError) as caught:
            builder.run_build(config, QUIET)
        self.assertIn("but DH0 is only", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWhdloadPrefs(unittest.TestCase):
    """WHDLoad runs its hooks around every game, on every launch."""

    PREFS = (b";WHDLoad preferences\n"
             b"ExecuteStartup=uae-configuration cachesize 0\n"
             b"ExecuteCleanup=uae-configuration cpu_speed max\n"
             b"PAL           ;force PAL video mode\n"
             b"QuitKey=$59\n"
             b";ExecutePreDisk=Execute S:WHDLoad-PreDisk\n")

    def clean(self) -> str:
        fixer = compat.Compatibility(Progress(), enabled=True, rtg=False,
                                     native=True)
        return fixer.offer("S/WHDLoad.prefs", self.PREFS).decode("latin-1")

    def test_emulator_hooks_are_commented_out(self):
        """uae-configuration does not exist on a PiStorm, so every launch
        would run something that is not there."""
        out = self.clean()
        self.assertIn(";ExecuteStartup=uae-configuration", out)
        self.assertIn(";ExecuteCleanup=uae-configuration", out)

    def test_the_rest_of_the_settings_are_kept(self):
        out = self.clean()
        self.assertIn("PAL           ;force PAL video mode", out)
        self.assertIn("QuitKey=$59", out)

    def test_an_already_commented_hook_is_not_commented_twice(self):
        self.assertIn(";ExecutePreDisk=Execute S:WHDLoad-PreDisk", self.clean())
        self.assertNotIn(";;ExecutePreDisk", self.clean())

    def test_a_hook_that_runs_something_real_is_left_alone(self):
        fixer = compat.Compatibility(Progress(), enabled=True, rtg=False,
                                     native=True)
        prefs = b"ExecuteStartup=Execute S:MyOwnScript\n"
        self.assertEqual(fixer.offer("S/WHDLoad.prefs", prefs), prefs)

    def test_nothing_is_touched_when_the_pass_is_off(self):
        fixer = compat.Compatibility(Progress(), enabled=False)
        self.assertEqual(fixer.offer("S/WHDLoad.prefs", self.PREFS), self.PREFS)


class TestTheGamesListIsNotCarriedOver(unittest.TestCase):
    """iGame's list holds an absolute path to every slave, written on
    somebody else's machine. Editing it to match this card meant guessing at
    another program's data; it is left off instead, and iGame builds its own
    from what is actually on the card.
    """

    def setUp(self):
        folder = Path(tempfile.mkdtemp(prefix="pistorm-igame-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        self.games = folder / "Games"
        (self.games / "WHDLOAD/OCS/D/Driller").mkdir(parents=True)
        (self.games / "WHDLOAD/OCS/D/Driller/Driller.slave").write_bytes(b"s")

    def fixer(self, *excludes: str) -> compat.Compatibility:
        fixer = compat.Compatibility(Progress(), enabled=True, rtg=False,
                                     native=True)
        fixer.content["GAMES"] = (self.games, excludes)
        return fixer

    def test_the_list_is_left_off_the_card(self):
        self.assertTrue(self.fixer().skip("Programs/iGame/gameslist.csv"))

    def test_it_is_left_off_wherever_it_sits(self):
        """iGame can be installed anywhere; the name identifies it."""
        self.assertTrue(self.fixer().skip("Tools/iGame/gameslist.csv"))

    def test_a_card_with_no_games_still_leaves_it_off(self):
        fixer = compat.Compatibility(Progress(), enabled=True)
        self.assertTrue(fixer.skip("Programs/iGame/gameslist.csv"))

    def test_the_repository_list_is_still_corrected(self):
        """That one is a short list of drawers to scan, and naming a drawer
        this card has not got sends iGame looking for something absent."""
        out = self.fixer("WHDLOAD/AGA").offer(
            "Programs/iGame/repos.prefs",
            b"Games:WHDLOAD/OCS/\nGames:WHDLOAD/AGA/\n").decode("latin-1")
        self.assertIn("OCS", out)
        self.assertNotIn("AGA", out)
