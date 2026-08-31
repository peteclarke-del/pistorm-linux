"""The version is stated once, and the packaging and the interface agree.

The two used to be written out separately - a literal in ``pyproject.toml`` and
another in the About dialog - with nothing to catch them drifting apart. That is
the sort of disagreement noticed only after a release has gone out, so these
tests guard the arrangement that removed it rather than the value itself.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pistorm_imager  # noqa: E402


class TestVersionIsStatedOnce(unittest.TestCase):
    def setUp(self):
        try:
            import tomllib
        except ModuleNotFoundError:   # 3.10 is supported; tomllib arrived in 3.11
            self.skipTest("reading pyproject.toml needs Python 3.11")
        self.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    def test_the_packaging_takes_the_version_from_the_package(self):
        project = self.pyproject["project"]
        self.assertNotIn("version", project,
                         "a literal version here can drift from the package's")
        self.assertIn("version", project.get("dynamic", []))
        attr = self.pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        self.assertEqual(attr, "pistorm_imager.__version__")

    def test_the_package_states_a_version(self):
        self.assertRegex(pistorm_imager.__version__, r"^\d+\.\d+\.\d+")

    def test_the_about_dialog_does_not_write_its_own(self):
        """The window reads the package's version rather than repeating it."""
        source = (ROOT / "pistorm_imager" / "ui" / "window.py").read_text()
        self.assertIn("version=__version__", source)
        self.assertIsNone(re.search(r'\bversion="\d+\.\d+\.\d+"', source),
                          "a version typed into the window will drift")


if __name__ == "__main__":
    unittest.main(verbosity=2)
