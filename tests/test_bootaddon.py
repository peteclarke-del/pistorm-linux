"""Add-ons installed onto the Emu68 boot partition.

The first of them, AGA-PISTORM, has two installation steps and the first is a
Windows batch file. This is that step, done from Linux: back up ``config.txt``,
make the partition writable from the Amiga, add the overlays the card has not
got, and copy the add-on's drawer onto it. The second step stays where its
author put it - an installer run on the Amiga, which replaces the kernel and
the RTG driver and asks questions this tool cannot answer.

**Nothing here downloads anything.** The real archive is behind itch.io's
"name your own price" page, which will not serve a file to anything but a
browser, so what the tool does is *find* one the user already has. These tests
build a stand-in laid out exactly as the add-on's own README describes it -
the same thing ``tests/test_gui_smoke.py`` does for an AmigaOS 3.9 disc it
cannot ship - so the discovery, the layout and the install are all exercised
against the real shape.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import (bootaddon, builder, fat32,  # noqa: E402
                                 machines, mbr)
from pistorm_imager.core.util import MIB, Progress  # noqa: E402

QUIET = Progress()

#  Overlays Emu68 1.1 ships, and one only the add-on has. The point of the
#  distinction is the whole of what the overlay step does.
EMU68_OVERLAYS = ("sdhc", "unicam")
ADDON_ONLY_OVERLAY = "aga"


def make_download(into: Path, version: str = "1.1") -> Path:
    """A stand-in AGA-PISTORM archive, laid out as its README describes.

    The wrapping matters and is reproduced: the Amiga drawer is two levels
    down, the Windows script is beside it, and there is a source tree that
    must not be mistaken for the drawer.
    """
    top = into / (f"AGA-Pistorm universal(1MB_2MB chip)"
                  f"_(Pistorm_Classic_16_600)v{version}")
    drawer = top / "Amiga" / "AGA-Pistorm"
    for folder in (top / "PC", top / "Licenses", top / "Source" / "Emu68",
                   drawer / "Files" / "Kernel",
                   drawer / "Files" / "Libs" / "Picasso96",
                   drawer / "Files" / "Overlays", drawer / "Files" / "C"):
        folder.mkdir(parents=True, exist_ok=True)
    (top / "README.txt").write_text("stand-in\n")
    (top / "SD-Setup.cmd").write_text("@echo off\n")
    (top / "PC" / "SD-Setup.ps1").write_text("# stand-in\n")
    (top / "Licenses" / "GPL-2.0.txt").write_text("GPL\n")
    (top / "Source" / "Emu68" / "aga.patch").write_text("diff\n")
    for name in ("Install", "Uninstall", "ReadMe.txt"):
        (drawer / name).write_text(name + "\n")
    (drawer / "Files" / "Kernel" / "Emu68-classic-aga.gz").write_bytes(
        b"\x1f\x8b" + b"K" * 4096)
    (drawer / "Files" / "Libs" / "Picasso96" / "VideoCore.card").write_bytes(
        b"V" * 2048)
    for name in EMU68_OVERLAYS + (ADDON_ONLY_OVERLAY,):
        (drawer / "Files" / "Overlays" / f"{name}.dtbo").write_bytes(b"D" * 512)
    (drawer / "Files" / "Overlays" / "overlays.md").write_text("notes\n")
    for name in ("agaboot", "agastat"):
        (drawer / "Files" / "C" / name).write_bytes(b"\x00\x00\x03\xf3" + b"A" * 512)
    (top / "Amiga" / "AGA-Pistorm.info").write_bytes(b"\xe3\x10" + b"I" * 300)
    archive = into / (top.name + ".7z")
    subprocess.run(["7z", "a", "-t7z", str(archive), str(top)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True)
    return archive


class _Scratch(unittest.TestCase):
    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-addon-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder

    def addon(self) -> bootaddon.BootAddon:
        """The one in the catalogue that wants a writable boot partition.

        Found by what it is rather than named, so this keeps working when
        there is more than one.
        """
        found = [a for a in bootaddon.CATALOGUE if a.writable_boot]
        self.assertTrue(found, "no add-on needs a writable boot partition")
        return found[0]


class WhatTheCardHasToBeForIt(_Scratch):
    """An add-on that emulates a chipset has requirements a real machine fails.

    None of these is a preference. AGA emulation on a machine that has AGA is
    pointless, and on an unexpanded A500 it does not run at all - so each is a
    refusal, and each refusal says which one it was rather than leaving a
    greyed-out switch to be guessed at.
    """

    def check(self, key="a500", chip_ram=2048, pi=machines.Pi.PI4,
              tag="v1.1.0-beta.1",
              accelerator=machines.Accelerator.PISTORM) -> str:
        machine = machines.MACHINES_BY_KEY[key]
        return self.addon().refusal(machine, accelerator=accelerator, pi=pi,
                                    chip_ram=chip_ram, emu68_tag=tag)

    def test_an_expanded_ecs_machine_on_a_pistorm_can_have_it(self):
        self.assertEqual(self.check(key="a500ecs"), "")
        self.assertEqual(self.check(key="a500plus", chip_ram=1024), "")
        self.assertEqual(self.check(key="a600", chip_ram=1024,
                                    pi=machines.Pi.CM4), "")

    def test_a_machine_that_already_has_aga_does_not_want_it(self):
        said = self.check(key="a1200")
        self.assertIn("AGA", said)

    def test_an_unexpanded_a500_has_not_the_chip_ram(self):
        stock = machines.MACHINES_BY_KEY["a500"].stock_chip_ram
        said = self.check(chip_ram=stock)
        self.assertIn("chip RAM", said)
        self.assertIn("512K", said)

    def test_an_emu68_too_old_refuses_it(self):
        said = self.check(tag="v1.0.7")
        self.assertIn("Emu68 1.1", said)

    def test_a_version_nobody_can_read_is_allowed_through(self):
        #  A build from a local zip or an unpacked folder carries no tag.
        self.assertEqual(self.check(tag=""), "")

    def test_without_a_pistorm_there_is_no_kernel_to_patch(self):
        said = self.check(accelerator=machines.Accelerator.STOCK)
        self.assertIn("PiStorm", said)

    def test_suits_agrees_with_what_the_refusal_says(self):
        #  Two answers to the same question is how they come apart, so the
        #  refusal is checked against the decision for every combination.
        for key in machines.MACHINES_BY_KEY:
            machine = machines.MACHINES_BY_KEY[key]
            for chip_ram in machine.chip_ram_options:
                for accelerator in machines.Accelerator:
                    with self.subTest(key=key, chip_ram=chip_ram,
                                      accelerator=accelerator.value):
                        args = dict(accelerator=accelerator,
                                    pi=machine.default_pi, chip_ram=chip_ram,
                                    emu68_tag="v1.1.0-beta.1")
                        self.assertEqual(
                            self.addon().suits(machine, **args),
                            self.addon().refusal(machine, **args) == "")


class FindingAnArchiveNobodyCanDownload(_Scratch):
    """It is published where only a browser can fetch it, so it is found.

    The same way a Kickstart or a PFS3 handler is: wherever the user keeps
    Amiga material. The name carries the version and the machines it supports
    and both have changed once already, so it is matched on the part that has
    not.
    """

    def test_the_newest_download_wins(self):
        folder = self.scratch()
        old = make_download(folder, "1.0")
        import os
        import time
        new = make_download(folder, "1.1")
        os.utime(old, (time.time() - 600, time.time() - 600))
        real = bootaddon.search_roots
        bootaddon.search_roots = lambda extra=None: [folder]
        try:
            self.assertEqual(bootaddon.find_archive(self.addon()), new)
        finally:
            bootaddon.search_roots = real

    def test_nothing_there_is_not_an_error(self):
        folder = self.scratch()
        real = bootaddon.search_roots
        bootaddon.search_roots = lambda extra=None: [folder]
        try:
            self.assertIsNone(bootaddon.find_archive(self.addon()))
        finally:
            bootaddon.search_roots = real

    def test_the_drawer_is_found_by_name_not_by_path(self):
        #  Today it is at Amiga/AGA-Pistorm with the Windows script beside it.
        #  That arrangement is the publisher's and has no reason to hold.
        folder = self.scratch()
        make_download(folder)
        unpacked = folder / "unpacked"
        unpacked.mkdir()
        subprocess.run(["7z", "x", "-y", f"-o{unpacked}",
                        str(next(folder.glob("*.7z")))],
                       stdout=subprocess.DEVNULL, check=True)
        found = bootaddon.locate_drawer(unpacked, self.addon())
        self.assertIsNotNone(found)
        self.assertEqual(found.name, self.addon().drawer)
        self.assertTrue((found / "Install").is_file())

    def test_a_deeper_copy_of_the_name_does_not_win(self):
        root = self.scratch()
        (root / "Amiga" / self.addon().drawer).mkdir(parents=True)
        (root / "Source" / "x" / self.addon().drawer).mkdir(parents=True)
        found = bootaddon.locate_drawer(root, self.addon())
        self.assertEqual(found, root / "Amiga" / self.addon().drawer)


class WhatGoesOntoTheBootPartition(_Scratch):
    """The Windows step, done here, read back off the partition afterwards."""

    def boot(self, size: int = 96 * MIB):
        path = self.scratch() / "boot.img"
        with open(path, "wb") as handle:
            handle.truncate(size)
        subprocess.run(["mkfs.vfat", "-F", "32", "-n", "EMU68BOOT", str(path)],
                       check=True, capture_output=True)
        handle = open(path, "r+b")
        self.addCleanup(handle.close)
        fs = fat32.Fat32(handle)
        #  A card as Emu68 1.1 leaves it: a config.txt and its own overlays.
        fs.write_bytes("config.txt", b"kernel=Emu68-pistorm-classic\n")
        fs.makedirs(bootaddon.OVERLAYS)
        for name in EMU68_OVERLAYS:
            fs.write_bytes(f"{bootaddon.OVERLAYS}/{name}.dtbo", b"EMU68")
        return fs

    def unpacked(self) -> Path:
        folder = self.scratch()
        archive = make_download(folder)
        out = folder / "unpacked"
        out.mkdir()
        subprocess.run(["7z", "x", "-y", f"-o{out}", str(archive)],
                       stdout=subprocess.DEVNULL, check=True)
        return out

    def test_the_drawer_and_its_icon_are_copied_whole(self):
        fs, addon = self.boot(), self.addon()
        bootaddon.install(fs, addon, self.unpacked(), QUIET)
        for path in (f"{addon.drawer}/Install",
                     f"{addon.drawer}/Uninstall",
                     f"{addon.drawer}/Files/Kernel/Emu68-classic-aga.gz",
                     f"{addon.drawer}/Files/Libs/Picasso96/VideoCore.card",
                     f"{addon.drawer}/Files/C/agaboot",
                     f"{addon.drawer}.info"):
            with self.subTest(path):
                self.assertTrue(fs.exists(path), path)

    def test_the_icon_matters_because_step_two_is_a_double_click(self):
        #  A drawer with no icon cannot be opened from Workbench, and opening
        #  it is the whole of the step this hands over to.
        fs, addon = self.boot(), self.addon()
        bootaddon.install(fs, addon, self.unpacked(), QUIET)
        self.assertTrue(fs.exists(f"{addon.drawer}.info"))

    def test_config_txt_is_backed_up_before_anything_changes_it(self):
        fs, addon = self.boot(), self.addon()
        before = fs.read_bytes("config.txt")
        bootaddon.install(fs, addon, self.unpacked(), QUIET)
        self.assertEqual(fs.read_bytes(f"config.txt.{bootaddon.BACKUP_SUFFIX}"),
                         before)

    def test_running_it_twice_keeps_the_first_backup(self):
        #  The backup is what somebody puts back when the Amiga will not
        #  start. Overwriting it on a second run with a file that has already
        #  been changed is how they lose the thing they are meant to restore.
        fs, addon = self.boot(), self.addon()
        original = fs.read_bytes("config.txt")
        unpacked = self.unpacked()
        bootaddon.install(fs, addon, unpacked, QUIET)
        fs.write_bytes("config.txt", b"kernel=Emu68-classic-aga\n")
        bootaddon.install(fs, addon, unpacked, QUIET)
        self.assertEqual(fs.read_bytes(f"config.txt.{bootaddon.BACKUP_SUFFIX}"),
                         original)

    def test_only_the_overlays_the_card_has_not_got_are_added(self):
        #  The ones it has came with the Emu68 release it was built from and
        #  are the ones its kernel was built against.
        fs, addon = self.boot(), self.addon()
        bootaddon.install(fs, addon, self.unpacked(), QUIET)
        for name in EMU68_OVERLAYS:
            with self.subTest(name):
                self.assertEqual(
                    fs.read_bytes(f"{bootaddon.OVERLAYS}/{name}.dtbo"),
                    b"EMU68", f"{name} was replaced")
        self.assertTrue(fs.exists(
            f"{bootaddon.OVERLAYS}/{ADDON_ONLY_OVERLAY}.dtbo"))

    def test_a_download_that_is_not_the_right_one_is_refused(self):
        fs, addon = self.boot(), self.addon()
        empty = self.scratch()
        (empty / "something-else").mkdir()
        with self.assertRaises(ValueError) as caught:
            bootaddon.install(fs, addon, empty, QUIET)
        self.assertIn(addon.drawer, str(caught.exception))

    def test_a_boot_partition_too_small_says_so_and_writes_nothing(self):
        #  Half-installing a boot partition is worse than leaving it alone.
        fs, addon = self.boot(size=8 * MIB), self.addon()
        unpacked = self.unpacked()
        drawer = bootaddon.locate_drawer(unpacked, addon)
        big = drawer / "Files" / "Kernel" / "huge.bin"
        big.write_bytes(b"x" * (32 * MIB))
        with self.assertRaises(ValueError) as caught:
            bootaddon.install(fs, addon, unpacked, QUIET)
        self.assertIn("boot partition", str(caught.exception))
        self.assertFalse(fs.exists(f"{addon.drawer}.info"))


class TheCardIsMadeWritableWhateverTheSwitchSaid(_Scratch):
    """The add-on's own installer runs on the Amiga and writes to this partition.

    Emu68 1.1 mounts the boot partition read-only unless the command line says
    otherwise, so without this the last step of the installation fails - on the
    Amiga, long after this tool has stopped watching. The interface holds the
    switch on while such an add-on is chosen; this is the rule underneath it,
    which a build from a saved job or the command line gets as well.
    """

    def addon_key(self) -> str:
        return next(a.key for a in bootaddon.CATALOGUE if a.writable_boot)

    def config(self, **kwargs) -> builder.BuildConfig:
        settings = dict(machine_key="a500", chip_ram=2048, pi_model="pi4",
                        release_tag="v1.1.0-beta.1",
                        boot_addons=[self.addon_key()])
        settings.update(kwargs)
        return builder.BuildConfig(target="/tmp/x", **settings)

    def test_it_is_needed_when_such_an_addon_is_chosen(self):
        self.assertTrue(self.config().needs_writable_boot())

    def test_and_not_otherwise(self):
        self.assertFalse(self.config(boot_addons=[]).needs_writable_boot())

    def test_an_addon_the_card_cannot_take_does_not_ask_for_it(self):
        #  A saved job can name one the machine it is loaded against cannot
        #  have, and writing a kernel's drawer onto a card it is not for is
        #  worse than leaving it out.
        stock = machines.MACHINES_BY_KEY["a500"].stock_chip_ram
        config = self.config(chip_ram=stock)
        self.assertEqual(config.chosen_addons(), [])
        self.assertFalse(config.needs_writable_boot())

    def test_it_reaches_the_cmdline_on_a_written_card(self):
        #  Read off the card rather than off the configuration: this is the
        #  step that decides whether the Amiga can finish the installation.
        folder = self.scratch()
        make_download(folder)
        target = folder / "card.img"
        real = bootaddon.search_roots
        bootaddon.search_roots = lambda extra=None: [folder]
        try:
            builder.run_build(builder.BuildConfig(
                mode=builder.BuildMode.FRESH, target=str(target),
                image_size=700 * MIB, boot_only=True, amiga_partitions=[],
                install_emu68=True, variant="pistorm",
                release_tag="v1.1.0-beta.1",
                machine_key="a500", chip_ram=2048, pi_model="pi4",
                boot_addons=[self.addon_key()]), QUIET)
        except RuntimeError as error:                 # no network for Emu68
            self.skipTest(f"needs the Emu68 release: {error}")
        finally:
            bootaddon.search_roots = real

        with open(target, "rb") as handle:
            parts = mbr.read_table(handle)
            fs = fat32.Fat32(handle, parts[0].start_bytes)
            cmdline = fs.read_bytes("cmdline.txt").decode()
            self.assertIn("sd.unit0=rw", cmdline)
            addon = bootaddon.CATALOGUE_BY_KEY[self.addon_key()]
            self.assertTrue(fs.exists(f"{addon.drawer}.info"))
            self.assertTrue(fs.exists(
                f"{addon.drawer}/Files/Kernel/Emu68-classic-aga.gz"))
            #  The backup is of the config.txt this build wrote, not of one
            #  from some earlier card: it is taken after the file is final.
            self.assertEqual(
                fs.read_bytes(f"config.txt.{bootaddon.BACKUP_SUFFIX}"),
                fs.read_bytes("config.txt"))


class HowMuchChipRamIsFitted(unittest.TestCase):
    """The chipset's own memory, which an Agnus decides the size of.

    Nothing needed this until an add-on that emulates a chipset did. It is a
    fact about somebody's machine rather than anything derivable, which is why
    it is asked for rather than assumed.
    """

    def test_every_machine_says_what_it_can_have(self):
        for machine in machines.MACHINES:
            with self.subTest(machine.key):
                self.assertTrue(machine.chip_ram_options)
                self.assertEqual(machine.stock_chip_ram,
                                 machine.chip_ram_options[0])

    def test_an_unexpanded_a500_has_half_a_megabyte(self):
        self.assertEqual(machines.MACHINES_BY_KEY["a500"].stock_chip_ram, 512)

    def test_the_machines_that_shipped_with_a_megabyte_say_so(self):
        for key in ("a500plus", "a600"):
            with self.subTest(key):
                self.assertEqual(
                    machines.MACHINES_BY_KEY[key].stock_chip_ram, 1024)

    def test_an_a1200_has_two_and_cannot_have_less(self):
        a1200 = machines.MACHINES_BY_KEY["a1200"]
        self.assertEqual(a1200.chip_ram_options, (2048,))

    def test_a_size_the_machine_cannot_have_falls_back_to_stock(self):
        a500 = machines.MACHINES_BY_KEY["a500"]
        self.assertEqual(a500.chip_ram_fitted(2048), 2048)
        self.assertEqual(a500.chip_ram_fitted(4096), a500.stock_chip_ram)
        self.assertEqual(a500.chip_ram_fitted(0), a500.stock_chip_ram)

    def test_it_is_said_the_way_an_amiga_owner_says_it(self):
        self.assertEqual(machines.chip_ram_label(512), "512K")
        self.assertEqual(machines.chip_ram_label(1024), "1 MB")
        self.assertEqual(machines.chip_ram_label(2048), "2 MB")
        self.assertIn("chipset", machines.chip_ram_label(0))
