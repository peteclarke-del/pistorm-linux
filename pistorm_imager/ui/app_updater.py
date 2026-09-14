"""Checking for a newer PiStorm Imager, from the About dialog.

The window keeps one ``AppUpdater``, so an answer stays shown when the About
dialog is closed and reopened, and a check cannot be started twice. Nothing is
checked until the user presses Check for Application Updates, or chooses the
menu item of the same name, which opens the dialog and presses it. A check
that fails says why and never says "newest".

A release publishes no package, so there is nothing for the program to
download or install: a newer release is answered with the release page and
the command that updates this copy (see ``core.updates``).
"""
from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import APPLICATION_NAME, __version__  # noqa: E402
from ..core import updates  # noqa: E402

CHECK_LABEL = "_Check for Application Updates"
PAGE_LABEL = "Open Release _Page"


@dataclasses.dataclass(frozen=True)
class AppUpdateState:
    #  "idle", "checking", "current", "available" or "failed"
    phase: str
    message: str = ""
    release: updates.Release | None = None

    @property
    def busy(self) -> bool:
        return self.phase == "checking"


def open_uri(widget: Gtk.Widget, uri: str) -> None:
    """Open ``uri`` in the user's browser, on behalf of ``widget``'s window."""
    root = widget.get_root()
    Gtk.UriLauncher(uri=uri).launch(root if isinstance(root, Gtk.Window) else None,
                                    None, None)


class AppUpdater:
    """The application update check, shared by the About dialog and the menu."""

    def __init__(self, check: Callable[[], updates.Release | None] = updates.check,
                 where: updates.Installation | None = None) -> None:
        self._check = check
        self._where = where or updates.installation()
        self.state = AppUpdateState("idle")
        self._listeners: list[Callable[[AppUpdateState], None]] = []

    def subscribe(self, listener: Callable[[AppUpdateState], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[AppUpdateState], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _set(self, state: AppUpdateState) -> None:
        self.state = state
        for listener in list(self._listeners):
            listener(state)

    def available_text(self, release: updates.Release) -> str:
        return (f"{release.name} is available. You have version {__version__}. "
                f"{updates.how_to_update(release, self._where)}")

    def check(self) -> None:
        """Ask GitHub, off the interface thread, and show the answer."""
        if self.state.busy:
            return
        self._set(AppUpdateState("checking", "Asking GitHub for the newest version"))

        def found(release: updates.Release | None) -> bool:
            if release is None:
                newest = f"{APPLICATION_NAME} {__version__} is the newest version"
                self._set(AppUpdateState("current", newest))
            else:
                self._set(AppUpdateState("available", self.available_text(release),
                                         release))
            return False

        def failed(error: BaseException) -> bool:
            message = f"Could not check for a newer version: {error}"
            self._set(AppUpdateState("failed", message))
            return False

        def work() -> None:
            try:
                release = self._check()
            except Exception as error:  # noqa: BLE001 - every failure is shown
                GLib.idle_add(failed, error)
            else:
                GLib.idle_add(found, release)

        threading.Thread(target=work, name="app-update-check", daemon=True).start()


class AppUpdateControls(Gtk.Box):
    """The About dialog's button and status line for the application update."""

    def __init__(self, updater: AppUpdater) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         halign=Gtk.Align.CENTER, margin_top=6)
        self.add_css_class("app-update")
        self._updater = updater
        self.button = Gtk.Button(use_underline=True, halign=Gtk.Align.CENTER)
        self.button.add_css_class("pill")
        self.button.connect("clicked", self._on_button)
        self.append(self.button)
        self.status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                max_width_chars=44, selectable=True)
        self.status.add_css_class("dim-label")
        self.append(self.status)
        updater.subscribe(self.show)
        self.show(updater.state)

    def detach(self) -> None:
        """Stop following the updater, when the About dialog closes."""
        self._updater.unsubscribe(self.show)

    def show(self, state: AppUpdateState) -> None:
        self.status.set_text(state.message)
        self.status.set_visible(bool(state.message))
        self.button.set_sensitive(not state.busy)
        if state.phase == "checking":
            self.button.set_label("Checking")
        elif state.phase == "available" and state.release is not None:
            self.button.set_label(PAGE_LABEL)
        else:
            self.button.set_label(CHECK_LABEL)

    def _on_button(self, _button) -> None:
        state = self._updater.state
        if state.phase == "available" and state.release is not None:
            open_uri(self, state.release.url)
        else:
            self._updater.check()


def attach_to_about(about: Adw.AboutDialog, controls: Gtk.Widget) -> bool:
    """Put ``controls`` under the version on the About dialog's first page.

    Adw.AboutDialog has no place for extra widgets, so this finds its version
    button by the "app-version" style class and adds the controls after it.
    False when it is not there, which the GUI smoke test catches on the
    libadwaita in use.
    """
    root = about.get_child() or about
    version = _find(root, lambda widget: widget.has_css_class("app-version"))
    parent = version.get_parent() if version is not None else None
    if not isinstance(parent, Gtk.Box):
        return False
    parent.insert_child_after(controls, version)
    return True


def _find(widget: Gtk.Widget,
          wanted: Callable[[Gtk.Widget], bool]) -> Gtk.Widget | None:
    if wanted(widget):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find(child, wanted)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None
