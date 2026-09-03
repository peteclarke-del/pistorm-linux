"""Prepare PiStorm/Emu68 SD cards on Linux.

The version lives here and nowhere else.  ``pyproject.toml`` reads it from this
attribute and the About dialog imports it, because when the two were written out
separately they drifted: a release went out with the packaging saying one thing
and the window saying another, and nothing could have caught it.

Nothing heavier belongs in this module.  setuptools reads the attribute while
building, and the GTK imports the interface needs are not available then.
"""

__version__ = "0.3.0"
