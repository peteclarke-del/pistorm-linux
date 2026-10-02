"""Checking for a newer PiStorm Imager, from the About dialog.

The window keeps one ``AppUpdater``, so an answer stays shown when the About
dialog is closed and reopened, and a check cannot be started twice. Nothing is
checked until the user presses Check for Application Updates, or chooses the
menu item of the same name, which opens the dialog and presses it. A check
that fails says why and never says "newest".

A copy installed from a release package downloads and installs the newer
package from here, then offers to restart into it. Any other copy - a git
checkout, a pipx install - is answered with the release page and the command
that updates it (see ``core.updates``). Nothing is installed while a card is
being written, and the restart waits for the write as well.
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
CANCEL_LABEL = "_Cancel"
RESTART_LABEL = f"_Restart {APPLICATION_NAME}"
BUSY_WRITING = "The update can be installed once the card has been written."
RESTART_WHILE_WRITING = ("A card is being written; restart once it has "
                         "finished.")


@dataclasses.dataclass(frozen=True)
class AppUpdateState:
    #  "idle", "checking", "current", "available", "downloading",
    #  "installing", "installed" or "failed"
    phase: str
    message: str = ""
    release: updates.Release | None = None
    fraction: float | None = None

    @property
    def busy(self) -> bool:
        return self.phase in ("checking", "downloading", "installing")


class UpdateHost:
    """What the updater asks of the window. Nothing is written by default."""

    def writing(self) -> bool:
        return False

    def restart(self) -> bool:
        """Close and start again; False when that cannot happen now."""
        return False


def open_uri(widget: Gtk.Widget, uri: str) -> None:
    """Open ``uri`` in the user's browser, on behalf of ``widget``'s window."""
    root = widget.get_root()
    Gtk.UriLauncher(uri=uri).launch(root if isinstance(root, Gtk.Window) else None,
                                    None, None)


class AppUpdater:
    """The application update check, shared by the About dialog and the menu."""

    def __init__(self, check: Callable[[], updates.Release | None] | None = None,
                 where: updates.Installation | None = None,
                 host: UpdateHost | None = None,
                 download=updates.download, install=updates.install) -> None:
        self._where = where or updates.installation()
        self._check = check or (
            lambda: updates.check(target=self._where.target))
        self._host = host or UpdateHost()
        self._download = download
        self._install = install
        self._cancel: threading.Event | None = None
        self.state = AppUpdateState("idle")
        self._listeners: list[Callable[[AppUpdateState], None]] = []

    def set_host(self, host: UpdateHost) -> None:
        self._host = host

    @property
    def can_install(self) -> bool:
        """Whether this copy installs updates itself."""
        return self._where.kind == "package"

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
        if self.state.busy or self.state.phase == "installed":
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

    def update(self, release: updates.Release) -> None:
        """Download ``release``'s package and install it, off the main thread."""
        if self.state.busy or not release.installable:
            return
        if self._host.writing():
            self._set(AppUpdateState("available", BUSY_WRITING, release))
            return
        cancel = self._cancel = threading.Event()
        self._set(AppUpdateState("downloading", f"Downloading {release.name}",
                                 release, 0.0))

        def progress(done: int, total: int | None) -> None:
            fraction = done / total if total else None
            GLib.idle_add(self._downloading, release, fraction, done, total)

        def installing() -> bool:
            self._set(AppUpdateState(
                "installing", f"Installing {release.name} - your password is "
                "asked for, because the package is installed for everyone",
                release))
            return False

        def installed() -> bool:
            self._cancel = None
            self._set(AppUpdateState(
                "installed", f"{release.name} is installed. Restart to use it.",
                release))
            return False

        def failed(error: BaseException) -> bool:
            self._cancel = None
            if isinstance(error, updates.UpdateCancelled) or cancel.is_set():
                message = (str(error) if isinstance(error, updates.UpdateCancelled)
                           else "The download was cancelled.")
            else:
                message = f"The update failed: {error}"
            self._set(AppUpdateState("available", message, release))
            return False

        def work() -> None:
            try:
                package = self._download(release, progress, cancel)
                if cancel.is_set():
                    raise updates.UpdateCancelled("The download was cancelled.")
                #  Asked again: a write may have started while it downloaded.
                if self._host.writing():
                    raise updates.UpdateError(BUSY_WRITING)
                GLib.idle_add(installing)
                self._install(package)
            except Exception as error:  # noqa: BLE001 - every failure is shown
                GLib.idle_add(failed, error)
            else:
                GLib.idle_add(installed)

        threading.Thread(target=work, name="app-update", daemon=True).start()

    def _downloading(self, release: updates.Release, fraction: float | None,
                     done: int, total: int | None) -> bool:
        if self.state.phase == "downloading":
            size = (f"{done // 1024} of {total // 1024} KiB" if total
                    else f"{done // 1024} KiB")
            self._set(AppUpdateState("downloading",
                                     f"Downloading {release.name} - {size}",
                                     release, fraction))
        return False

    def cancel(self) -> None:
        """Stop the download; an install already under way cannot be."""
        if self._cancel is not None and self.state.phase == "downloading":
            self._cancel.set()
            self._set(dataclasses.replace(self.state,
                                          message="Cancelling the download"))

    def restart(self) -> None:
        if not self._host.restart():
            self._set(dataclasses.replace(self.state,
                                          message=RESTART_WHILE_WRITING))


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
        self.progress = Gtk.ProgressBar(visible=False)
        self.append(self.progress)
        updater.subscribe(self.show)
        self.show(updater.state)

    def detach(self) -> None:
        """Stop following the updater, when the About dialog closes."""
        self._updater.unsubscribe(self.show)

    def show(self, state: AppUpdateState) -> None:
        self.status.set_text(state.message)
        self.status.set_visible(bool(state.message))
        downloading = state.phase == "downloading"
        self.progress.set_visible(downloading or state.phase == "installing")
        if state.fraction is not None:
            self.progress.set_fraction(state.fraction)
        elif state.phase == "installing":
            self.progress.pulse()
        #  The one thing a busy update can be asked to do is stop downloading.
        self.button.set_sensitive(not state.busy or downloading)
        if state.phase == "checking":
            self.button.set_label("Checking")
        elif downloading:
            self.button.set_label(CANCEL_LABEL)
        elif state.phase == "installing":
            self.button.set_label("Installing")
        elif state.phase == "installed":
            self.button.set_label(RESTART_LABEL)
        elif state.phase == "available" and state.release is not None:
            self.button.set_label(
                f"_Update to {state.release.version}"
                if self._updater.can_install and state.release.installable
                else PAGE_LABEL)
        else:
            self.button.set_label(CHECK_LABEL)

    def _on_button(self, _button) -> None:
        state = self._updater.state
        if state.phase == "downloading":
            self._updater.cancel()
        elif state.phase == "installed":
            self._updater.restart()
        elif state.phase == "available" and state.release is not None:
            if self._updater.can_install and state.release.installable:
                self._confirm(state.release)
            else:
                open_uri(self, state.release.url)
        else:
            self._updater.check()

    def _confirm(self, release: updates.Release) -> None:
        """Say what is about to be installed, with its notes, before it is."""
        dialog = Adw.AlertDialog(
            heading=f"Update to {release.name}?",
            body=(release.notes or "Its notes are on the release page.")
            + "\n\nThe package is downloaded, checked against its published "
              "checksum, and installed for everyone on this computer, so your "
              "password is asked for.")
        dialog.add_response("cancel", "_Not Now")
        dialog.add_response("update", "_Download and Install")
        dialog.set_response_appearance("update",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("update")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, answer: (
            self._updater.update(release) if answer == "update" else None))
        dialog.present(self)


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
