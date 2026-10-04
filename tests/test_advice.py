"""Software that is a poor choice on this card or this AmigaOS: said, never silent."""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import builder, machines, packages  # noqa: E402
from pistorm_imager.core.machines import Display  # noqa: E402

OsAdvice = packages.OsAdvice


class WhichReleases(unittest.TestCase):
    """Release numbers are not in the order the releases came out."""

    def test_a_named_release_covers_its_updates(self):
        advice = OsAdvice("x", on=("3.2",))
        self.assertTrue(advice.applies((3, 2)))
        self.assertTrue(advice.applies((3, 2, 3)))

    def test_3_5_is_not_later_than_3_2(self):
        """3.5 and 3.9 are years older than 3.2, whatever the numbers say."""
        advice = OsAdvice("x", on=("3.5", "3.9"))
        self.assertFalse(advice.applies((3, 2)))
        self.assertTrue(advice.applies((3, 9)))

    def test_a_requirement_is_everything_older(self):
        advice = OsAdvice("x", below="3.0")
        self.assertTrue(advice.applies((1, 3)))
        self.assertTrue(advice.applies((2, 1)))
        self.assertFalse(advice.applies((3, 0)))
        self.assertFalse(advice.applies((3, 9)))

    def test_an_unknown_release_gets_no_advice(self):
        """A drive brought from elsewhere: no advice rather than made-up advice."""
        self.assertFalse(OsAdvice("x", on=("3.2",), below="9.9").applies(None))
        self.assertIsNone(packages.os_release(""))
        self.assertEqual(packages.os_release("3.1.4"), (3, 1, 4))


class EveryReasonIsSourced(unittest.TestCase):
    """Advice the user is asked to act on has to be checkable."""

    def test_each_has_a_reason_and_where_it_was_read(self):
        for package in packages.CATALOGUE:
            for advice in package.os_advice:
                where = f"{package.key}: {advice.why}"
                self.assertTrue(advice.why.endswith("."), where)
                self.assertTrue(advice.on or advice.below, where)
                self.assertTrue(advice.source.startswith("https://"), where)
                for named in advice.on + ((advice.below,) if advice.below
                                          else ()):
                    self.assertIsNotNone(packages.os_release(named), where)


def config(**given) -> builder.BuildConfig:
    given.setdefault("machine_key", "a1200")
    return builder.BuildConfig(target="/tmp/card.img", variant="pistorm",
                               rtg_display=True, **given)


class TheReleaseBeingInstalled(unittest.TestCase):

    def test_from_a_cd(self):
        made = config(os_cd="/tmp/AmigaOS3.2CD.iso", os_cd_release="3.2")
        self.assertEqual(made.os_release(), (3, 2))

    def test_from_floppies(self):
        made = config(install_amigaos=True, adf_folder="/tmp/adf",
                      adf_version="3.1")
        self.assertEqual(made.os_release(), (3, 1))

    def test_a_drive_from_elsewhere_is_not_guessed(self):
        self.assertIsNone(config(system_source="pimiga").os_release())


class ToldNotRefused(unittest.TestCase):

    def test_advice_on_this_release_is_said_before_the_build(self):
        made = config(os_cd="/tmp/AmigaOS3.2CD.iso", os_cd_release="3.2",
                      package_keys=["fblit"])
        said = made.advised_against()
        self.assertIn("FBlit", said)
        self.assertTrue(any("3.2" in reason for reason in said["FBlit"]))
        self.assertTrue(any("FBlit is not advised" in c
                            for c in made.concerns()))

    def test_the_same_package_on_another_release_is_not_advised_against(self):
        made = config(os_cd="/tmp/AmigaOS3.9.iso", os_cd_release="3.9",
                      package_keys=["fblit"])
        self.assertNotIn("FBlit", made.advised_against())

    def test_installed_anyway_is_not_left_out(self):
        """The user was told it does not suit, and chose it: their word holds."""
        a500 = dict(machine_key="a500", package_keys=["cardreset"])
        self.assertIn("CardReset", config(**a500).unsuited_packages())
        self.assertNotIn("CardReset", config(
            **a500, against_advice=["cardreset"]).unsuited_packages())

    def test_installed_anyway_reaches_the_card(self):
        a500 = machines.MACHINES_BY_KEY["a500"]
        package = packages.CATALOGUE_BY_KEY["cardreset"]
        self.assertFalse(package.suits(a500.chipset, Display.RTG_HDMI,
                                       machine=a500))
        def fetch(package, *_args):
            return [(f"cache/{package.key}", f"C/{package.key}")]
        with unittest.mock.patch.object(packages, "fetch", fetch):
            refused = packages.overlays_by_package(
                ["cardreset"], chipset=a500.chipset,
                display=Display.RTG_HDMI, machine=a500)
            insisted = packages.overlays_by_package(
                ["cardreset"], chipset=a500.chipset,
                display=Display.RTG_HDMI, machine=a500,
                insisted=["cardreset"])
        self.assertEqual(refused, [])
        #  And what it needs, which came with the choice.
        self.assertEqual(sorted(key for key, _pairs in insisted),
                         sorted(packages.expand(["cardreset"])))

    def test_why_is_said_in_words(self):
        a500 = machines.MACHINES_BY_KEY["a500"]
        said = packages.CATALOGUE_BY_KEY["cardreset"].advice(
            a500.chipset, Display.RTG_HDMI, machine=a500, release=(1, 3))
        self.assertTrue(any("PCMCIA" in reason for reason in said))
        self.assertTrue(any("AmigaOS 2.0" in reason for reason in said))

    def test_a_suggestion_does_not_overrule_the_advice(self):
        a1200 = machines.MACHINES_BY_KEY["a1200"]
        on_32 = packages.suggested(a1200, Display.RTG_HDMI, release=(3, 2))
        for key in on_32:
            self.assertEqual(
                packages.CATALOGUE_BY_KEY[key].os_reasons((3, 2)), [], key)


class SoftwareThatClashes(unittest.TestCase):
    """Two chosen packages that interact badly: said, on either of them."""

    def test_two_doing_the_same_job(self):
        said = packages.CATALOGUE_BY_KEY["lwip"].clash_reasons(["roadshow"])
        self.assertEqual(len(said), 1)
        self.assertIn("Roadshow", said[0])

    def test_a_clash_is_read_both_ways(self):
        for package in packages.CATALOGUE:
            for clash in package.clashes:
                other = packages.CATALOGUE_BY_KEY[clash.other]
                self.assertTrue(other.clash_reasons([package.key]),
                                f"{other.key} beside {package.key}")
                self.assertTrue(package.clash_reasons([other.key]),
                                f"{package.key} beside {other.key}")

    def test_what_a_package_needs_is_never_a_clash(self):
        """FText runs on FBlit."""
        self.assertEqual(
            packages.CATALOGUE_BY_KEY["ftext"].clash_reasons(["fblit"]), [])

    def test_each_names_a_package_a_reason_and_a_source(self):
        for package in packages.CATALOGUE:
            for clash in package.clashes:
                where = f"{package.key} / {clash.other}"
                self.assertIn(clash.other, packages.CATALOGUE_BY_KEY, where)
                self.assertNotEqual(clash.other, package.key, where)
                self.assertTrue(clash.why.endswith("."), where)
                self.assertTrue(clash.source.startswith("https://"), where)

    def test_the_build_says_it_once_with_the_reason(self):
        made = config(package_keys=["roadshow", "lwip"])
        said = made.advised_against()
        self.assertEqual(len(said), 1, said)
        self.assertTrue(any("not advised" in c and "bsdsocket" in c
                            for c in made.concerns()))

    def test_a_suggestion_never_holds_a_clash(self):
        for machine in machines.MACHINES:
            for display in Display:
                chosen = packages.expand(packages.suggested(
                    machine, display, networking=True))
                for key in chosen:
                    self.assertEqual(
                        packages.CATALOGUE_BY_KEY[key].clash_reasons(
                            [k for k in chosen if k != key]), [],
                        f"{key} on {machine.key}, {display.name}")

    def test_a_clash_with_what_the_display_holds_falls_on_the_other(self):
        """Picasso96 on an RTG screen is not the user's to leave off."""
        made = config(native_display=True, package_display="both",
                      package_keys=["blazewcp", "picasso96"])
        said = made.advised_against()
        self.assertIn("BlazeWCP", said)
        self.assertNotIn("Picasso96", said)


class WhatTheReleaseCarries(unittest.TestCase):
    """A requirement the AmigaOS being installed already meets is met."""

    def test_picasso96_does_not_bring_an_older_datatype_onto_3_2(self):
        self.assertEqual(packages.expand(["picasso96"], (3, 2)), ["picasso96"])
        self.assertIn("picturedt43", packages.expand(["picasso96"], (3, 1)))

    def test_asked_for_by_name_it_is_kept(self):
        self.assertIn("picturedt43",
                      packages.expand(["picasso96", "picturedt43"], (3, 2)))

    def test_the_build_installs_picasso96_without_it(self):
        made = config(os_cd="/tmp/AmigaOS3.2CD.iso", os_cd_release="3.2",
                      package_keys=["picasso96"])
        self.assertNotIn("picture.datatype V43", made.advised_against())

        def fetch(package, *_args):
            return [(f"cache/{package.key}", f"Libs/{package.key}")]
        with unittest.mock.patch.object(packages, "fetch", fetch):
            got = packages.overlays_by_package(
                ["picasso96"], display=Display.BOTH, release=(3, 2),
                machine=machines.MACHINES_BY_KEY["a1200"])
        self.assertEqual([key for key, _pairs in got], ["picasso96"])

    def test_only_what_the_release_really_carries_is_marked(self):
        for package in packages.CATALOGUE:
            for advice in package.os_advice:
                if advice.provided:
                    self.assertTrue(advice.on, package.key)


class WhatAPackageBrings(unittest.TestCase):
    """A tick never brings something the advice then warns against unsaid."""

    RELEASES = [(1, 3), (2, 0), (2, 1), (3, 0), (3, 1), (3, 2), (3, 5),
                (3, 9)]

    def test_a_package_carries_the_advice_of_what_it_needs(self):
        for package in packages.CATALOGUE:
            for release in self.RELEASES:
                if package.os_reasons(release):
                    continue
                for key in packages.expand([package.key], release):
                    self.assertEqual(
                        packages.CATALOGUE_BY_KEY[key].os_reasons(release),
                        [], f"{package.key} on {release} brings {key}")

    def test_cardreset_alone_on_3_2(self):
        """The 3.2 FAQ recommends CardReset alone, and that is possible."""
        self.assertEqual(packages.expand(["cardreset"], (3, 2)),
                         ["cardreset"])
        self.assertEqual(
            packages.CATALOGUE_BY_KEY["cardreset"].os_reasons((3, 2)), [])
        self.assertEqual(
            packages.CATALOGUE_BY_KEY["pcmciacd"].os_reasons((3, 2)), [])


if __name__ == "__main__":
    unittest.main()
