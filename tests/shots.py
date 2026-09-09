"""Render the README's screenshots from the real window.

Not a test: it builds the actual application, walks it to each screen and
renders the window offscreen to `docs/images/`.  Captured by hand, the pictures
in a README drift away from the program as soon as anything moves; produced
from the running window they can be regenerated whenever it changes.

    python3 tests/shots.py        # needs a display

The window is drawn through GTK's own renderer rather than photographed off the
desktop, so nothing depends on a compositor, a theme or a screenshot tool, and
no window has to be brought to the front.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#  The real configuration belongs to whoever is running this: a shot of the
#  window must not show, or disturb, their saved session.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="pistorm-shots-config-")

from pistorm_imager.app import ImagerApplication  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from pistorm_imager.core import rdb  # noqa: E402

OUT = ROOT / "docs" / "images"
SCRATCH = Path(tempfile.mkdtemp(prefix="pistorm-shots-"))
EXPORT_IMAGE = SCRATCH / "card-backup.img"

WIDTH = 900
HEIGHT = 760


def make_export_image() -> None:
    """A small card image with three named PFS3 drives for Export to list."""
    geometry = rdb.Geometry()
    cylinder = geometry.cyl_blocks * geometry.block_size
    total = 40 * cylinder
    dostype = rdb.parse_dostype("PFS3")
    table = rdb.Rdb(
        geometry=geometry,
        partitions=rdb.layout(geometry, total // geometry.block_size,
                              [("DH0", 8 * cylinder, dostype),
                               ("DH1", 12 * cylinder, dostype),
                               ("DH2", None, dostype)]),
        #  A handler in the RDB, because that is what Export copies into each
        #  file and what makes the result mountable on its own.
        filesystems=[rdb.FileSystem(dostype=dostype, seglist=b"handler" * 128,
                                    version=19 << 16 | 2)],
        cylinders=total // cylinder)
    with open(EXPORT_IMAGE, "wb") as handle:
        handle.truncate(total)
        table.write(handle, 0)


def settle(milliseconds: int = 400) -> None:
    """Let real time pass, so the frame clock actually ticks.

    Draining pending events is not enough: a widget that has just been shown
    renders as nothing until it has been given a frame, and the snapshot comes
    back empty.
    """
    context = GLib.MainContext.default()
    done = []
    GLib.timeout_add(milliseconds, lambda: (done.append(True), False)[1])
    while not done:
        context.iteration(True)


def shot(window, name: str, height: int = HEIGHT) -> None:
    window.set_default_size(WIDTH, height)
    window.queue_draw()
    settle(500)
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, WIDTH, height)
    node = snapshot.to_node()
    if node is None:
        raise RuntimeError(f"{name}: the window rendered nothing")
    texture = window.get_renderer().render_texture(node, None)
    texture.save_to_png(str(OUT / f"{name}.png"))
    print(f"wrote docs/images/{name}.png")


def on_activate(app: ImagerApplication) -> None:
    try:
        window = app.window
        window.present()
        settle(900)

        shot(window, "01-welcome")

        #  Taller: this screen carries the masthead, the machine and the plan,
        #  and the detections are the point of the picture.
        window._choose_basic()
        settle(700)
        shot(window, "02-quick-setup", height=1180)

        window._set_customising(True)
        settle(500)
        for page, name in (("source", "03-source"), ("storage", "04-storage"),
                           ("amiga", "05-amiga"), ("packages", "06-packages"),
                           ("options", "07-options"), ("target", "08-target")):
            window.stack.set_visible_child_name(page)
            settle(450)
            shot(window, name)

        window._choose_export()
        settle(450)
        window.export_source.set_path(str(EXPORT_IMAGE))
        settle(900)
        shot(window, "09-export-drives")
    except Exception:                             # noqa: BLE001 - report and quit
        import traceback
        traceback.print_exc()
        app.quit()
        raise SystemExit(1)
    finally:
        app.quit()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    make_export_image()
    #  Never share the running application's id, or this hands its activation
    #  to that instance and photographs nothing.
    app = ImagerApplication("org.pistorm.ImagerShots", unique=False)
    app.connect_after("activate", on_activate)
    app.run([])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
