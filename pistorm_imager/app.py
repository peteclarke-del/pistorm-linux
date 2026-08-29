"""Application entry point."""
from __future__ import annotations

import os
import sys

#  Terminals inside a snap (VS Code, for instance) export paths to that snap's
#  bundled GTK loaders.  A system Python that then dlopens one of them pulls the
#  snap's glibc into the process and dies with a symbol lookup error the moment
#  GTK renders its first symbolic icon.  Nothing here is ours to keep, so drop
#  the snap's variables before GTK is imported.
if "SNAP_INSTANCE_NAME" in os.environ or "/snap/" in os.environ.get("GTK_EXE_PREFIX", ""):
    for _name in ("GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR", "GTK_EXE_PREFIX",
                  "GTK_PATH", "GTK_IM_MODULE_FILE", "GIO_MODULE_DIR",
                  "LOCPATH", "GSETTINGS_SCHEMA_DIR"):
        if "/snap/" in os.environ.get(_name, "") or "snap" in os.environ.get(_name, ""):
            os.environ.pop(_name, None)
    _ld = os.environ.get("LD_LIBRARY_PATH", "")
    if "/snap/" in _ld:
        kept = [p for p in _ld.split(":") if p and "/snap/" not in p]
        if kept:
            os.environ["LD_LIBRARY_PATH"] = ":".join(kept)
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from .ui.window import ImagerWindow  # noqa: E402

APP_ID = "org.pistorm.Imager"


class ImagerApplication(Adw.Application):
    def __init__(self, application_id: str = APP_ID, unique: bool = True):
        #  Tests pass their own id: with the shared one, a copy of the imager
        #  already running would take the activation and the test would quietly
        #  do nothing at all.
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        if not unique:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=application_id, flags=flags)
        self.window: ImagerWindow | None = None

    def do_activate(self) -> None:  # noqa: D102 - GObject vfunc naming
        if self.window is None:
            self.window = ImagerWindow(self)
        self.window.present()

    def do_command_line(self, command_line) -> int:  # noqa: D102
        self.activate()
        return 0


def main(argv: list[str] | None = None) -> int:
    return ImagerApplication().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
