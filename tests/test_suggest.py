"""Which AmigaOS suits a machine: worked out, not looked up."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import amigacd, kickstart, machines, suggest  # noqa: E402
from pistorm_imager.core.machines import Accelerator, Cpu, Display  # noqa: E402


def rom(version: int, revision: int, aga: bool | None) -> kickstart.RomInfo:
    return kickstart.RomInfo(path=Path(f"/roms/{version}.{revision}-{aga}.rom"),
                             size=524288, version=version, revision=revision,
                             name=f"Kickstart {version}.{revision}", aga=aga,
                             encrypted=False, byte_swapped=False, sha1="x",
                             usable=True)


ROMS = [rom(40, 68, True), rom(40, 63, False), rom(47, 96, True),
        rom(47, 96, False)]
A1200 = machines.MACHINES_BY_KEY["a1200"]
A500 = machines.MACHINES_BY_KEY["a500"]


class WhatEachReleaseSays(unittest.TestCase):

    def test_every_release_says_when_it_came_out(self):
        """The order the numbers give is not the order they came in."""
        for choice in suggest.choices():
            self.assertGreater(choice.year, 1980, choice.key)
        years = {c.key: c.year for c in suggest.choices()}
        self.assertLess(years["3.9"], years["3.2"])

    def test_3_2_is_best_on_its_own_rom(self):
        release = amigacd.RELEASES_BY_KEY["3.2"]
        self.assertEqual(release.best_kickstart, 47)
        self.assertLess(release.kickstart_from, release.best_kickstart)


class WhatSuits(unittest.TestCase):

    def test_a_pistorm_gets_the_newest_release_on_its_own_rom(self):
        found = suggest.suggest(A1200, Accelerator.PISTORM, Display.BOTH,
                                roms=ROMS, cd_release="3.2")
        self.assertEqual(found.choice.key, "3.2")
        self.assertEqual((found.rom.version, found.rom.aga), (47, True))
        self.assertEqual(found.missing, [])
        self.assertTrue(found.packages)

    def test_a_stock_68000_gets_its_own_release(self):
        found = suggest.suggest(A500, Accelerator.STOCK, Display.NATIVE,
                                roms=ROMS, adf_versions=["3.1"])
        self.assertEqual(found.choice.key, "3.1")
        self.assertEqual((found.rom.version, found.rom.aga), (40, False))

    def test_what_needs_a_68020_is_never_offered_to_a_68000(self):
        found = suggest.suggest(A500, Accelerator.STOCK, Display.NATIVE)
        self.assertTrue(Cpu.M68000.at_least(found.choice.needs_cpu))

    def test_an_rtg_screen_needs_a_3_x_release(self):
        found = suggest.suggest(A500, Accelerator.STOCK, Display.RTG_HDMI,
                                adf_versions=["1.3"])
        self.assertGreaterEqual(found.choice.best_kickstart, 39)

    def test_what_is_missing_is_said_and_what_is_here_is_offered(self):
        found = suggest.suggest(A1200, Accelerator.PISTORM, Display.BOTH,
                                roms=ROMS, adf_versions=["3.1"])
        self.assertEqual(found.choice.key, "3.2")
        self.assertTrue(any("CD" in m for m in found.missing))
        self.assertIsNotNone(found.instead)
        self.assertEqual(found.instead.choice.key, "3.1")
        self.assertEqual(found.instead.missing, [])

    def test_its_disc_supplies_its_rom(self):
        """The 3.2 CD carries its Kickstarts; with it, no ROM is missing."""
        found = suggest.suggest(A1200, Accelerator.PISTORM, Display.BOTH,
                                roms=[rom(40, 68, True)], cd_release="3.2")
        self.assertEqual(found.missing, [])

    def test_the_load_follows_the_release(self):
        """Nothing advised against on the release suggested."""
        found = suggest.suggest(A1200, Accelerator.PISTORM, Display.BOTH,
                                roms=ROMS, cd_release="3.2")
        from pistorm_imager.core import packages     # noqa: PLC0415
        for key in found.packages:
            self.assertEqual(
                packages.CATALOGUE_BY_KEY[key].os_reasons(found.release), [],
                key)


A600 = machines.MACHINES_BY_KEY["a600"]


class TheRomThatIsFitted(unittest.TestCase):
    """The chip in the machine decides - unless it is a PiStorm's file."""

    def suggest(self, accelerator, fitted, card=Cpu.M68020, machine=None):
        return suggest.suggest(machine or A600, accelerator, Display.NATIVE,
                               card_cpu=card, roms=ROMS,
                               adf_versions=["3.1"], cd_release="3.9",
                               fitted_rom=fitted)

    def test_a_3_1_chip_and_a_68020_card_gets_3_9(self):
        found = self.suggest(Accelerator.ACCELERATOR, rom(40, 63, False))
        self.assertEqual(found.choice.key, "3.9")
        self.assertEqual(found.rom.version, 40, "the chip, not another file")
        self.assertEqual(found.missing, [])

    def test_a_3_2_chip_gets_3_2_and_never_what_needs_3_1(self):
        found = self.suggest(Accelerator.ACCELERATOR, rom(47, 96, False))
        self.assertEqual(found.choice.key, "3.2")
        self.assertIsNone(found.instead, "3.9 cannot run on a 3.2 ROM")

    def test_3_2_on_a_3_1_chip_comes_after_what_was_made_for_it(self):
        cpu = Cpu.M68020
        runs = [c for c in suggest.choices()
                if cpu.at_least(c.needs_cpu) and suggest._runs_on(c, 40)]
        ranked = [c.key for c in suggest._ranked(runs, A600,
                                                 Accelerator.ACCELERATOR,
                                                 cpu, 40)]
        self.assertLess(ranked.index("3.1"), ranked.index("3.2"))
        self.assertEqual(ranked[0], "3.9")

    def test_a_stock_machine_with_its_3_1_chip_gets_3_1(self):
        found = self.suggest(Accelerator.STOCK, rom(40, 63, False), card=None)
        self.assertEqual(found.choice.key, "3.1")

    def test_a_pistorm_is_not_held_to_a_rom(self):
        """Its Kickstart is a file: any release's own ROM can be had."""
        found = self.suggest(Accelerator.PISTORM, rom(40, 68, True),
                             machine=A1200)
        self.assertEqual(found.choice.key, "3.2")
        self.assertEqual(found.rom.version, 47)

    def test_without_the_rom_it_asks_for_it(self):
        found = self.suggest(Accelerator.ACCELERATOR, None)
        self.assertTrue(any("Kickstart is fitted" in r for r in found.reasons))


if __name__ == "__main__":
    unittest.main()
