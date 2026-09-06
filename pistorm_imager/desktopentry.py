"""Putting the launcher where the desktop can find it.

``pipx install`` - which is how this is meant to be installed, from a published
tag rather than a working copy - puts the Python package in a virtual
environment and **nothing anywhere else**. It has no idea about
``~/.local/share/applications`` or the hicolor icon theme, and neither does
``pip``. So an installed copy had no menu entry and no icon: the application
appeared in the desktop's grid as a generic drive, if it appeared at all.

The two files were in the repository all along, and the documented way to
install them was a pair of ``install -Dm644`` lines run from a checkout - which
is exactly what somebody installing from a tag does not have. They now travel
inside the package, and this puts them where the XDG specification says they
go.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#  Where the packaged copies live, laid out the way they will be installed.
SOURCE = Path(__file__).resolve().parent / "data"
ICON_THEME = "hicolor"


def data_home() -> Path:
    """``$XDG_DATA_HOME``, or the default the specification gives for it."""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def _icons(root: Path) -> list[tuple[Path, Path]]:
    icons = SOURCE / "icons"
    return [(path, root / "icons" / path.relative_to(icons))
            for path in sorted(icons.rglob("*")) if path.is_file()]


def planned(root: Path | None = None) -> list[tuple[Path, Path]]:
    """Every (source, destination) this would write, without writing any."""
    root = root or data_home()
    entry = SOURCE / "applications" / "pistorm-imager.desktop"
    return [(entry, root / "applications" / entry.name)] + _icons(root)


def install(root: Path | None = None, log=print) -> list[Path]:
    """Copy the desktop entry and the icon into place. Returns what was written."""
    root = root or data_home()
    written: list[Path] = []
    for source, destination in planned(root):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        #  Readable by the desktop, and not executable: a .desktop file with
        #  the executable bit set is treated as a script by some file managers.
        destination.chmod(0o644)
        written.append(destination)
        log(f"  {destination}")
    _refresh(root, log)
    return written


def _refresh(root: Path, log=print) -> None:
    """Tell the desktop the files are there.

    Both are best-effort. A missing tool is not a failure: the desktop notices
    a new ``.desktop`` file on its own, in its own time, and an icon theme with
    no cache is read by scanning it. Neither is worth refusing to install over.
    """
    for argv in (["update-desktop-database", str(root / "applications")],
                 ["gtk-update-icon-cache", "-f", "-t",
                  str(root / "icons" / ICON_THEME)]):
        if shutil.which(argv[0]) is None:
            continue
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode == 0:
            log(f"  refreshed with {argv[0]}")
        else:
            log(f"  {argv[0]} said: {(result.stderr or result.stdout).strip()}")
