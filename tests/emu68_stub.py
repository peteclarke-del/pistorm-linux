"""A stand-in Emu68 for tests that build a card and do not care which Emu68.

A card whose boot partition has no Emu68 on it cannot start, and the build
refuses to make one. Tests that are about something else - the Amiga drives,
an imported image - used to switch Emu68 off to avoid a download; they point
at this folder instead, which the build takes as an already unpacked release.
"""
import tempfile
from pathlib import Path


def emu68_folder() -> str:
    folder = Path(tempfile.mkdtemp(prefix="pistorm-emu68-stub-"))
    (folder / "Emu68-pistorm").write_bytes(b"\0" * 4096)
    (folder / "config.txt").write_text("kernel=Emu68-pistorm\n")
    return str(folder)


EMU68 = emu68_folder()
