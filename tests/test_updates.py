"""Deciding whether a newer release of this tool has been published."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager import APPLICATION_NAME, __version__  # noqa: E402
from pistorm_imager.core import updates  # noqa: E402


class TestVersions(unittest.TestCase):
    def test_a_tag_is_read_as_its_numbers(self):
        self.assertEqual(updates.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updates.parse_version("0.2.0"), (0, 2, 0))

    def test_only_a_vX_Y_Z_tag_is_a_version(self):
        """The releases are tagged vX.Y.Z; anything else is not one of them."""
        for tag in ("nightly", "", "v1.0", "0.3", "v1.2.3-beta", "v1.2.3.4", "release-1.2.3"):
            self.assertIsNone(updates.parse_version(tag), tag)

    def test_newer_and_older(self):
        self.assertTrue(updates.is_newer("0.3.0", "0.2.0"))
        self.assertTrue(updates.is_newer("v1.0.0", "0.9.9"))
        self.assertTrue(updates.is_newer("v0.10.0", "0.9.0"))
        self.assertFalse(updates.is_newer("0.1.0", "0.2.0"))

    def test_the_same_version_is_not_newer(self):
        self.assertFalse(updates.is_newer("0.2.0", "0.2.0"))
        self.assertFalse(updates.is_newer("v0.2.0", "0.2.0"))

    def test_an_unparseable_tag_never_claims_to_be_newer(self):
        """Better to say nothing than to announce an update that is not one."""
        self.assertFalse(updates.is_newer("latest"))
        self.assertFalse(updates.is_newer(""))
        self.assertFalse(updates.is_newer("v99.0"))

    def test_it_compares_against_this_build_by_default(self):
        self.assertFalse(updates.is_newer(__version__))
        self.assertFalse(updates.is_newer(f"v{__version__}"))

    def test_the_repository_it_asks_about(self):
        self.assertEqual(updates.REPO, "peteclarke-del/pistorm-linux")
        self.assertEqual(updates.LATEST_URL,
                         "https://api.github.com/repos/peteclarke-del/pistorm-linux"
                         "/releases/latest")
        self.assertEqual(updates.RELEASES_PAGE,
                         "https://github.com/peteclarke-del/pistorm-linux/releases")


#  A reply from GitHub for a release later than this one.
NEWER = {"tag_name": "v99.0.0", "name": "v99.0.0 - it flies now",
         "html_url": "https://github.com/peteclarke-del/pistorm-linux/releases/tag/v99.0.0"}


class TestCheck(unittest.TestCase):
    """What a reply from GitHub is taken to mean."""

    def check_with(self, reply, current=__version__):
        asked = []

        def fetch(url):
            asked.append(url)
            return reply

        return updates.check(current, fetch=fetch), asked

    def test_it_asks_for_the_release_marked_latest(self):
        """GitHub never marks a draft or a prerelease as the latest release."""
        _found, asked = self.check_with(NEWER)
        self.assertEqual(asked, [updates.LATEST_URL])

    def test_a_newer_release_is_offered(self):
        found, _asked = self.check_with(NEWER)
        self.assertEqual(found, updates.Release("99.0.0", "v99.0.0", NEWER["html_url"]))
        self.assertEqual(found.name, f"{APPLICATION_NAME} 99.0.0")

    def test_this_version_and_older_ones_are_not(self):
        self.assertIsNone(self.check_with({"tag_name": f"v{__version__}"})[0])
        self.assertIsNone(self.check_with({"tag_name": "v0.0.1"})[0])

    def test_a_release_without_a_page_points_at_the_list(self):
        found, _asked = self.check_with({"tag_name": "v99.0.0"})
        self.assertEqual(found.url, updates.RELEASES_PAGE)

    def test_no_release_at_all_is_an_error_not_an_answer(self):
        with self.assertRaisesRegex(updates.UpdateError, "No release has been published"):
            self.check_with(None)

    def test_a_latest_release_with_a_stray_tag_is_an_error(self):
        with self.assertRaisesRegex(updates.UpdateError, "nightly, is not tagged"):
            self.check_with({"tag_name": "nightly"})


class TestFetchLatest(unittest.TestCase):
    """The answer when the question cannot be answered is the reason."""

    def fetch_with(self, reply=None, error=None):
        requests = []

        @contextlib.contextmanager
        def fake(request, timeout=None):
            requests.append(request)
            if error is not None:
                raise error
            yield io.BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode())

        with mock.patch.object(updates.urllib.request, "urlopen", fake):
            found = updates.fetch_latest()
        return found, requests

    def http_error(self, code, body=b""):
        return urllib.error.HTTPError(updates.LATEST_URL, code, "reply", {}, io.BytesIO(body))

    def test_the_request_names_the_tool(self):
        found, requests = self.fetch_with({"tag_name": "v1.0.0"})
        self.assertEqual(found, {"tag_name": "v1.0.0"})
        self.assertEqual(requests[0].full_url, updates.LATEST_URL)
        self.assertEqual(requests[0].get_header("User-agent"), "pistorm-imager")
        self.assertEqual(requests[0].get_header("Accept"), "application/vnd.github+json")

    def test_no_published_release_is_none(self):
        self.assertIsNone(self.fetch_with(error=self.http_error(404))[0])

    def test_a_refusal_gives_githubs_reason(self):
        body = json.dumps({"message": "API rate limit exceeded"}).encode()
        with self.assertRaisesRegex(updates.UpdateError,
                                    r"HTTP 403: API rate limit exceeded"):
            self.fetch_with(error=self.http_error(403, body))

    def test_no_network_says_so(self):
        error = urllib.error.URLError("Temporary failure in name resolution")
        with self.assertRaisesRegex(updates.UpdateError,
                                    "api.github.com could not be reached: Temporary"):
            self.fetch_with(error=error)

    def test_a_timeout_says_so(self):
        with self.assertRaisesRegex(updates.UpdateError, "could not be reached: timed out"):
            self.fetch_with(error=TimeoutError())

    def test_an_unreadable_reply_says_so(self):
        with self.assertRaisesRegex(updates.UpdateError, "could not be read"):
            self.fetch_with(b"<html>")

    def test_a_reply_that_is_not_a_release_says_so(self):
        for reply in ([], {"message": "Not Found"}):
            with self.assertRaisesRegex(updates.UpdateError, "did not send a release"):
                self.fetch_with(reply)


class TestHowToUpdate(unittest.TestCase):
    """A release publishes no package, so the answer is how this copy is updated."""

    RELEASE = updates.Release("99.0.0", "v99.0.0", updates.RELEASES_PAGE)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pistorm-update-test-"))
        self.package = self.root / "tree" / "pistorm_imager"
        self.package.mkdir(parents=True)
        self.prefix = self.root / "prefix"
        self.prefix.mkdir()

    def test_this_repository_is_a_checkout(self):
        found = updates.installation()
        self.assertEqual(found, updates.Installation("checkout", updates.PACKAGE_DIR.parent))

    def test_a_checkout_is_updated_with_git(self):
        (self.package.parent / ".git").mkdir()
        where = updates.installation(self.package, str(self.prefix))
        self.assertEqual(where, updates.Installation("checkout", self.package.parent))
        text = updates.how_to_update(self.RELEASE, where)
        self.assertIn("runs from a git checkout", text)
        self.assertTrue(text.endswith(f"git -C {self.package.parent} pull"), text)

    def test_a_linked_worktree_is_a_checkout_too(self):
        (self.package.parent / ".git").write_text("gitdir: elsewhere\n")
        where = updates.installation(self.package, str(self.prefix))
        self.assertEqual(where.kind, "checkout")

    def test_a_folder_with_a_space_is_quoted(self):
        where = updates.Installation("checkout", Path("/home/me/Personal Projects/pistorm"))
        self.assertEqual(updates.update_command(self.RELEASE, where),
                         "git -C '/home/me/Personal Projects/pistorm' pull")

    def test_a_pipx_install_is_updated_with_pipx_from_the_new_tag(self):
        (self.prefix / "pipx_metadata.json").write_text("{}")
        where = updates.installation(self.package, str(self.prefix))
        self.assertEqual(where, updates.Installation("pipx"))
        text = updates.how_to_update(self.RELEASE, where)
        self.assertIn("installed with pipx", text)
        self.assertTrue(text.endswith(
            "pipx install --force --system-site-packages "
            "git+https://github.com/peteclarke-del/pistorm-linux@v99.0.0"), text)

    def test_anything_else_is_sent_to_the_release_page(self):
        where = updates.installation(self.package, str(self.prefix))
        self.assertEqual(where, updates.Installation("other"))
        self.assertEqual(updates.update_command(self.RELEASE, where), "")
        self.assertIn("release page", updates.how_to_update(self.RELEASE, where))

    def test_the_readme_install_command_is_the_one_pipx_is_given(self):
        """The command shown for a pipx copy is the README's, with --force."""
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
        self.assertIn(f'"{updates.INSTALL_SOURCE}@v', readme)
        self.assertIn("pipx install --system-site-packages", readme)


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
        """And the driver now has two publishers, which makes it matter more.

        Emu68 publishes ``VideoCore.card`` itself from 1.1 onwards, ahead of
        the copy inside Emu68-tools, so the cached file has to be checked
        against the source this build actually resolved rather than against
        either publisher's address.
        """
        from pistorm_imager.core import compat                # noqa: PLC0415
        import inspect                                        # noqa: PLC0415
        body = inspect.getsource(compat.fetch_videocore_card)
        reuse = body.split("if cache.exists")[1][:400]
        self.assertIn("note.read_text().strip() == url", reuse)
        self.assertIn("url, where = videocore_source()", body)

    def test_the_firmware_checks_what_actually_arrived(self):
        from pistorm_imager.core import emu68                 # noqa: PLC0415
        body = self.source_of(emu68)
        self.assertIn("Content-Length", body)
        self.assertIn("declared", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
