"""Deciding whether a newer release of this tool has been published."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager import __version__  # noqa: E402
from pistorm_imager.core import updates  # noqa: E402


class TestVersions(unittest.TestCase):
    def test_a_tag_is_read_as_its_numbers(self):
        self.assertEqual(updates.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updates.parse_version("0.2.0"), (0, 2, 0))

    def test_a_tag_with_no_numbers_sorts_as_nothing(self):
        self.assertEqual(updates.parse_version("nightly"), ())
        self.assertEqual(updates.parse_version(""), ())

    def test_newer_and_older(self):
        self.assertTrue(updates.is_newer("0.3.0", "0.2.0"))
        self.assertTrue(updates.is_newer("v1.0", "0.9.9"))
        self.assertFalse(updates.is_newer("0.1.0", "0.2.0"))

    def test_the_same_version_is_not_newer(self):
        self.assertFalse(updates.is_newer("0.2.0", "0.2.0"))
        self.assertFalse(updates.is_newer("v0.2.0", "0.2.0"))

    def test_a_shorter_tag_compares_by_value_not_length(self):
        """0.3 and 0.3.0 are the same version, written two ways."""
        self.assertFalse(updates.is_newer("0.3", "0.3.0"))
        self.assertFalse(updates.is_newer("0.3.0", "0.3"))

    def test_an_unparseable_tag_never_claims_to_be_newer(self):
        """Better to say nothing than to announce an update that is not one."""
        self.assertFalse(updates.is_newer("latest"))
        self.assertFalse(updates.is_newer(""))

    def test_it_compares_against_this_build_by_default(self):
        self.assertFalse(updates.is_newer(__version__))

    def test_the_repository_it_asks_about(self):
        self.assertIn("pistorm-linux", updates.RELEASES_API)
        self.assertTrue(updates.RELEASES_PAGE.startswith("https://github.com/"))


class TestLatest(unittest.TestCase):
    """The answer when the question cannot be answered."""

    def call_with(self, payload):
        import contextlib, io, json as _json  # noqa: PLC0415
        original = updates.urllib.request.urlopen

        @contextlib.contextmanager
        def fake(*_a, **_k):
            yield io.BytesIO(_json.dumps(payload).encode())

        updates.urllib.request.urlopen = fake
        try:
            return updates.latest()
        finally:
            updates.urllib.request.urlopen = original

    def test_the_newest_published_release_wins(self):
        found = self.call_with([
            {"tag_name": "v0.1.0", "name": "First", "body": "a"},
            {"tag_name": "v0.9.0", "name": "Newest", "body": "b"},
            {"tag_name": "v0.5.0", "name": "Middle", "body": "c"},
        ])
        self.assertEqual(found.tag, "v0.9.0")
        self.assertEqual(found.name, "Newest")

    def test_drafts_and_prereleases_are_ignored(self):
        found = self.call_with([
            {"tag_name": "v0.1.0", "name": "Real", "body": ""},
            {"tag_name": "v9.0.0", "name": "Draft", "draft": True},
            {"tag_name": "v8.0.0", "name": "Beta", "prerelease": True},
        ])
        self.assertEqual(found.tag, "v0.1.0")

    def test_a_repository_with_no_releases_answers_nothing(self):
        self.assertIsNone(self.call_with([]))

    def test_an_unexpected_payload_answers_nothing(self):
        self.assertIsNone(self.call_with({"message": "Not Found"}))


class EveryCacheKnowsWhereItCameFrom(unittest.TestCase):
    """Three rebuilds were lost to one shape of bug: something kept from a
    previous run and handed back although the thing it came from had changed.

    The archive cache, the unpacked tree, the RTG driver and the Raspberry Pi
    firmware were all keyed on a name or on mere existence. This walks the
    code and asserts that anything reusing a cached file checks where it came
    from, so the next one added has to as well.
    """

    def source_of(self, module):
        import inspect                                        # noqa: PLC0415
        return inspect.getsource(module)

    def test_the_archive_cache_records_its_source(self):
        from pistorm_imager.core import packages              # noqa: PLC0415
        body = self.source_of(packages)
        self.assertIn(".source", body)
        self.assertIn("came_from", body)

    def test_the_unpacked_tree_is_compared_with_its_archive(self):
        from pistorm_imager.core import packages              # noqa: PLC0415
        import inspect                                        # noqa: PLC0415
        body = inspect.getsource(packages.unpack)
        self.assertIn("st_mtime", body,
                      "an unpacked tree must be checked against its archive")

    def test_the_rtg_driver_records_which_release_it_came_from(self):
        from pistorm_imager.core import compat                # noqa: PLC0415
        import inspect                                        # noqa: PLC0415
        body = inspect.getsource(compat.fetch_videocore_card)
        self.assertIn("EMU68_TOOLS_URL", body.split("if cache.exists")[1][:400])

    def test_the_firmware_checks_what_actually_arrived(self):
        from pistorm_imager.core import emu68                 # noqa: PLC0415
        body = self.source_of(emu68)
        self.assertIn("Content-Length", body)
        self.assertIn("declared", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
