"""The application updating itself from a release package.

A release publishes a Debian package and SHA256SUMS. A copy installed from a
package downloads the newer one for its system, checks it, and installs it
through install-update under pkexec, which checks it again as root.
"""
import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.app import restart_command  # noqa: E402
from pistorm_imager.core import updates  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
TARGET = updates.PackageTarget("ubuntu24.04", "all",
                               "PiStorm-Imager_{version}_ubuntu24.04_all.deb")
PACKAGE = "PiStorm-Imager_9.0.0_ubuntu24.04_all.deb"


def release(tag="v9.0.0", assets=(PACKAGE, "SHA256SUMS"), base="http://x",
            body="Reads more formats."):
    return {"tag_name": tag, "html_url": f"{base}/releases/tag/{tag}",
            "body": body,
            "assets": [{"name": name, "browser_download_url": f"{base}/{name}",
                        "size": 1234} for name in assets]}


def shell(script: str) -> str:
    """Run ``script`` with package-target.sh sourced, and say what it printed."""
    return subprocess.run(
        ["bash", "-c", f'source "{PACKAGING}/package-target.sh"; {script}'],
        check=True, capture_output=True, text=True).stdout


class _Scratch(unittest.TestCase):
    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-update-test-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder


class TheRecordThePackageKeeps(_Scratch):

    def test_the_record_names_its_system_and_file(self):
        record = self.scratch() / "package-target"
        shell(f'write_package_target "{record}" all')
        target = updates.installed_target(record)
        self.assertEqual(target, TARGET)
        self.assertEqual(target.system, "Ubuntu 24.04")

    def test_the_update_asks_for_the_file_the_build_names(self):
        self.assertEqual(TARGET.package_name("9.0.0"),
                         shell('package_file_name 9.0.0 all'))

    def test_a_copy_with_no_record_or_a_broken_one_has_no_system(self):
        folder = self.scratch()
        self.assertIsNone(updates.installed_target(folder / "package-target"))
        (folder / "package-target").write_text("distro=ubuntu24.04\n")
        self.assertIsNone(updates.installed_target(folder / "package-target"))
        (folder / "package-target").write_text(
            "distro=x\narch=all\npackage=../../etc/passwd\n")
        self.assertIsNone(updates.installed_target(folder / "package-target"))

    def test_a_copy_with_a_record_is_a_package_install(self):
        top = self.scratch()
        (top / "pistorm_imager").mkdir()
        shell(f'write_package_target "{top}/package-target" all')
        where = updates.installation(top / "pistorm_imager")
        self.assertEqual((where.kind, where.target), ("package", TARGET))


class ReadingTheRelease(unittest.TestCase):

    def test_a_newer_release_with_this_systems_package_is_installable(self):
        found = updates.release_from(release(), "0.12.0", TARGET)
        self.assertTrue(found.installable)
        self.assertEqual((found.package_name, found.package_url, found.sums_url),
                         (PACKAGE, f"http://x/{PACKAGE}", "http://x/SHA256SUMS"))

    def test_the_same_or_an_older_version_is_not_offered(self):
        self.assertIsNone(updates.release_from(release("v0.12.0"), "0.12.0",
                                               TARGET))

    def test_without_this_systems_package_or_a_record_it_cannot_install(self):
        self.assertFalse(updates.release_from(
            release(assets=("Other.deb", "SHA256SUMS")), "0.12.0",
            TARGET).installable)
        self.assertFalse(updates.release_from(release(), "0.12.0",
                                              None).installable)

    def test_long_notes_are_shortened(self):
        found = updates.release_from(release(body="x" * 5000), "0.12.0",
                                     TARGET)
        self.assertLessEqual(len(found.notes), updates.NOTES_LIMIT + 3)

    def test_checksum_lines_are_read_as_sha256sum_writes_them(self):
        digest = "a" * 64
        self.assertEqual(updates.published_sum(f"{digest}  {PACKAGE}\n",
                                               PACKAGE), digest)
        self.assertEqual(updates.published_sum(f"{digest} *{PACKAGE}\n",
                                               PACKAGE), digest)
        self.assertEqual(updates.published_sum(f"{digest}  other.deb\n",
                                               PACKAGE), "")

    def test_a_package_copy_is_told_it_installs_from_here(self):
        found = updates.release_from(release(), "0.12.0", TARGET)
        where = updates.Installation("package", target=TARGET)
        self.assertIn("installed from here", updates.how_to_update(found, where))
        missing = updates.release_from(release(assets=("SHA256SUMS",)),
                                       "0.12.0", TARGET)
        self.assertIn("no package for Ubuntu 24.04",
                      updates.how_to_update(missing, where))


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


class Downloading(_Scratch):
    """Against a real web server on this machine."""

    def setUp(self):
        self.site = self.scratch()
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(_Quiet, directory=self.site))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cache = self.scratch()

    def publish(self, data=b"the package" * 1000, listed=True):
        (self.site / PACKAGE).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        (self.site / "SHA256SUMS").write_text(
            f"{digest}  {PACKAGE}\n" if listed else f"{digest}  other.deb\n")
        return updates.release_from(release(base=self.base), "0.12.0", TARGET)

    def test_the_package_is_downloaded_and_checked(self):
        found = self.publish()
        seen = []
        package = updates.download(found, lambda done, total: seen.append(done),
                                   folder=self.cache)
        self.assertEqual(package.path.read_bytes(),
                         (self.site / PACKAGE).read_bytes())
        self.assertEqual(package.sha256, hashlib.sha256(
            package.path.read_bytes()).hexdigest())
        self.assertTrue(seen, "progress is reported")

    def test_a_verified_package_already_there_is_not_downloaded_again(self):
        found = self.publish()
        first = updates.download(found, folder=self.cache)
        (self.site / PACKAGE).unlink()          # gone from the server
        self.assertEqual(updates.download(found, folder=self.cache), first)

    def test_a_package_that_does_not_match_its_checksum_is_removed(self):
        found = self.publish()
        (self.site / PACKAGE).write_bytes(b"something else")
        with self.assertRaisesRegex(updates.UpdateError, "checksum"):
            updates.download(found, folder=self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_a_package_the_checksums_do_not_list_is_not_downloaded(self):
        found = self.publish(listed=False)
        with self.assertRaisesRegex(updates.UpdateError, "does not list"):
            updates.download(found, folder=self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_a_cancelled_download_says_so_and_leaves_nothing(self):
        found = self.publish()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(updates.UpdateCancelled):
            updates.download(found, cancel=cancel, folder=self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_a_missing_package_says_so_and_leaves_nothing(self):
        found = self.publish()
        (self.site / PACKAGE).unlink()
        with self.assertRaisesRegex(updates.UpdateError, "HTTP 404"):
            updates.download(found, folder=self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_only_web_addresses_are_fetched(self):
        found = updates.Release("9.0.0", "v9.0.0", "x", PACKAGE,
                                "file:///etc/passwd", 0, "file:///etc/hosts")
        with self.assertRaisesRegex(updates.UpdateError, "Not a web address"):
            updates.download(found, folder=self.cache)


class Installing(_Scratch):

    def package(self) -> updates.VerifiedPackage:
        path = self.scratch() / PACKAGE
        path.write_bytes(b"package")
        return updates.VerifiedPackage(path, hashlib.sha256(b"package").hexdigest())

    def helper(self) -> Path:
        helper = self.scratch() / "install-update"
        helper.write_text("#!/bin/sh\n")
        return helper

    def run_returning(self, code: int, stderr: str = ""):
        calls = []

        def run(command, **_kw):
            calls.append(command)
            return types.SimpleNamespace(returncode=code, stderr=stderr)
        return run, calls

    def test_root_is_given_the_checksum_to_check_for_itself(self):
        if shutil.which("pkexec") is None:
            self.skipTest("no pkexec here")
        package, helper = self.package(), self.helper()
        run, calls = self.run_returning(0)
        updates.install(package, run=run, helper=helper)
        self.assertEqual(calls[0][1:], [str(helper), str(package.path),
                                        package.sha256])
        self.assertFalse(package.path.exists(), "the download is tidied away")

    def test_a_dismissed_password_prompt_installs_nothing(self):
        if shutil.which("pkexec") is None:
            self.skipTest("no pkexec here")
        run, _calls = self.run_returning(updates.PKEXEC_DISMISSED)
        with self.assertRaises(updates.UpdateCancelled):
            updates.install(self.package(), run=run, helper=self.helper())

    def test_a_refusal_or_a_failure_says_what_to_run_by_hand(self):
        if shutil.which("pkexec") is None:
            self.skipTest("no pkexec here")
        for code, said in ((updates.PKEXEC_REFUSED, "did not allow"),
                           (100, "E: broken")):
            run, _calls = self.run_returning(code, "E: broken\n")
            with self.subTest(code=code), \
                    self.assertRaisesRegex(updates.UpdateError,
                                           f"{said}.*sudo apt install"):
                updates.install(self.package(), run=run, helper=self.helper())

    def test_without_the_installer_the_command_is_given_instead(self):
        with self.assertRaisesRegex(updates.UpdateError, "sudo apt install"):
            updates.install(self.package(),
                            helper=self.scratch() / "not-there")


class TheRootSideInstaller(_Scratch):
    """packaging/install-update itself, with a stand-in apt-get."""

    def setUp(self):
        self.bin = self.scratch()
        self.log = self.scratch() / "apt.log"
        (self.bin / "apt-get").write_text(
            f'#!/bin/sh\necho "$@" > "{self.log}"\n'
            f'cp "$3" "{self.log}.deb"\n')
        (self.bin / "apt-get").chmod(0o755)

    def run_it(self, *args):
        return subprocess.run(
            [str(PACKAGING / "install-update"), *map(str, args)],
            capture_output=True, text=True,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"})

    def test_apt_is_given_a_verified_copy_and_the_copy_is_removed(self):
        package = self.scratch() / PACKAGE
        package.write_bytes(b"the package")
        result = self.run_it(package, hashlib.sha256(b"the package").hexdigest())
        self.assertEqual(result.returncode, 0, result.stderr)
        given = self.log.read_text().split()
        self.assertEqual(given[:2], ["install", "--yes"])
        self.assertNotEqual(given[2], str(package), "a copy, not the user's file")
        self.assertEqual(Path(f"{self.log}.deb").read_bytes(), b"the package")
        self.assertFalse(Path(given[2]).exists(), "the copy is removed after")

    def test_a_package_changed_after_the_check_is_not_installed(self):
        package = self.scratch() / PACKAGE
        package.write_bytes(b"swapped")
        result = self.run_it(package, hashlib.sha256(b"original").hexdigest())
        self.assertEqual(result.returncode, 65)
        self.assertFalse(self.log.exists())

    def test_anything_but_a_sha256_is_refused_before_the_file_is_touched(self):
        for bad in ("", "abc", "Z" * 64, "a" * 63):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_it("/nonexistent", bad).returncode, 64)


class TheBuild(_Scratch):
    """packaging/build-deb.sh, built and read back."""

    def test_the_package_installs_the_record_and_installer_where_looked_for(self):
        if shutil.which("dpkg-deb") is None:
            self.skipTest("no dpkg-deb here")
        out = self.scratch()
        built = subprocess.run([str(PACKAGING / "build-deb.sh"), str(out)],
                               check=True, capture_output=True, text=True)
        package = Path(built.stdout.strip().splitlines()[-1])
        from pistorm_imager import __version__
        self.assertEqual(package.name, TARGET.package_name(__version__))
        tree = self.scratch()
        subprocess.run(["dpkg-deb", "-x", str(package), str(tree)], check=True)
        lib = tree / "usr/lib/pistorm-imager"
        self.assertEqual(updates.installed_target(lib / "package-target"),
                         TARGET)
        self.assertEqual(updates.installation(lib / "pistorm_imager").kind,
                         "package")
        helper = lib / "bin" / "install-update"
        self.assertEqual(oct(helper.stat().st_mode & 0o777), "0o755")
        self.assertTrue((tree / "usr/bin/pistorm-imager").is_file())
        self.assertTrue(
            (tree / "usr/share/applications/pistorm-imager.desktop").is_file())


class Restarting(unittest.TestCase):

    def test_the_new_version_starts_with_the_same_arguments(self):
        self.assertEqual(restart_command(["/usr/bin/x", "--flag"]),
                         [sys.executable, "-m", "pistorm_imager", "--flag"])

    def test_a_restart_replaces_the_process(self):
        from pistorm_imager import app
        started = []

        class Fake:
            def __init__(self):
                self.restart_requested = True

            def run(self, _argv):
                return 0

        with unittest.mock.patch.object(app, "ImagerApplication", Fake), \
                unittest.mock.patch.object(app.os, "execv",
                                           lambda *a: started.append(a)):
            app.main(["pistorm-imager"])
        self.assertEqual(started[0][1], restart_command(["pistorm-imager"]))


class TheReleaseWorkflow(unittest.TestCase):

    def test_it_publishes_the_package_and_its_checksums(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("packaging/build-deb.sh dist", text)
        self.assertIn("sha256sum -- *.deb > SHA256SUMS", text)
        self.assertIn("packaging/check-release-tag.sh", text)

    def test_the_tag_must_say_the_version(self):
        from pistorm_imager import __version__
        ok = subprocess.run([str(PACKAGING / "check-release-tag.sh"),
                             f"v{__version__}"], capture_output=True)
        bad = subprocess.run([str(PACKAGING / "check-release-tag.sh"),
                              "v0.0.1"], capture_output=True)
        self.assertEqual((ok.returncode, bad.returncode), (0, 1))


if __name__ == "__main__":
    unittest.main()
