"""Small reusable pieces of the interface."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk, Pango  # noqa: E402


class FileRow(Adw.EntryRow):
    """A row that takes a path, either typed in or chosen from a dialog.

    An entry rather than a label because a folder is genuinely hard to pick in
    the GTK file dialog: you have to highlight it from its parent, and once you
    have opened it there is nothing selected and the Open button greys out.
    Anyone who knows the path they want should be able to paste it.
    """

    def __init__(self, title: str, subtitle: str = "", *, folder: bool = False,
                 both: bool = False,
                 filters: list[tuple[str, list[str]]] | None = None,
                 on_change: Callable[[str], None] | None = None):
        super().__init__(title=title)
        self._placeholder = subtitle or "None selected"
        self._folder = folder
        self._both = both
        self._filters = filters or []
        self._on_change = on_change
        self.path: str = ""
        self._echo = False               # guard against our own edits

        #  Where either a file or a folder is a valid answer there have to be
        #  two buttons: one GTK dialog cannot offer both, and a file chooser
        #  simply opens into any folder you click rather than selecting it.
        if both:
            self._folder_button = Gtk.Button(label="Folder…",
                                             valign=Gtk.Align.CENTER)
            self._folder_button.connect("clicked", self._on_clicked_folder)
            self.add_suffix(self._folder_button)
        self._button = Gtk.Button(label="File…" if both else "Choose…",
                                  valign=Gtk.Align.CENTER)
        self._button.connect("clicked", self._on_clicked)
        self._clear = Gtk.Button(icon_name="edit-clear-symbolic",
                                 valign=Gtk.Align.CENTER, tooltip_text="Clear")
        self._clear.add_css_class("flat")
        self._clear.set_visible(False)
        self._clear.connect("clicked", lambda _b: self.set_path(""))
        self.add_suffix(self._clear)
        self.add_suffix(self._button)
        self.connect("changed", self._on_typed)
        #  The description that used to be the subtitle: an entry row has no
        #  room for one, so it becomes the tooltip rather than being lost.
        self.set_tooltip_text(self._placeholder)

    #  Kept so callers that used the ActionRow API still read naturally.
    def set_subtitle(self, text: str) -> None:
        self.set_tooltip_text(text or "")

    def _on_typed(self, _entry) -> None:
        if self._echo:
            return
        typed = self.get_text().strip()
        #  A path pasted with surrounding quotes, or a file:// URI from a file
        #  manager, is what a drag or a copy actually gives you.
        if len(typed) > 1 and typed[0] == typed[-1] and typed[0] in "\"'":
            typed = typed[1:-1]
        if typed.startswith("file://"):
            typed = unquote(urlparse(typed).path)
        self.path = typed
        self._clear.set_visible(bool(typed))
        if self._on_change:
            self._on_change(self.path)

    def set_path(self, path: str) -> None:
        self.path = path or ""
        self._echo = True
        self.set_text(self.path)
        self._echo = False
        self._clear.set_visible(bool(self.path))
        if self._on_change:
            self._on_change(self.path)

    def _on_clicked(self, _button) -> None:
        dialog = Gtk.FileDialog(title=self.get_title())
        if self._filters:
            store = Gio.ListStore.new(Gtk.FileFilter)
            for name, patterns in self._filters:
                filt = Gtk.FileFilter()
                filt.set_name(name)
                for pattern in patterns:
                    filt.add_pattern(pattern)
                store.append(filt)
            everything = Gtk.FileFilter()
            everything.set_name("All files")
            everything.add_pattern("*")
            store.append(everything)
            dialog.set_filters(store)
        if self.path:
            existing = Path(self.path)
            parent = existing if existing.is_dir() else existing.parent
            if parent.exists():
                dialog.set_initial_folder(Gio.File.new_for_path(str(parent)))

        window = self.get_root()
        if self._folder:
            dialog.select_folder(window, None, self._finish)
        else:
            dialog.open(window, None, self._finish)

    def _on_clicked_folder(self, _button) -> None:
        dialog = Gtk.FileDialog(title=self.get_title())
        if self.path:
            existing = Path(self.path)
            parent = existing if existing.is_dir() else existing.parent
            if parent.exists():
                dialog.set_initial_folder(Gio.File.new_for_path(str(parent)))
        dialog.select_folder(self.get_root(), None, self._finish_folder)

    def _finish_folder(self, dialog, result) -> None:
        try:
            file = dialog.select_folder_finish(result)
        except Exception:  # noqa: BLE001 - the user cancelled
            return
        if file is not None:
            self.set_path(file.get_path())

    def _finish(self, dialog, result) -> None:
        try:
            file = (dialog.select_folder_finish(result)
                    if self._folder and not self._both
                    else dialog.open_finish(result))
        except Exception:  # noqa: BLE001 - the user cancelled
            return
        if file is not None:
            self.set_path(file.get_path())


class SaveRow(FileRow):
    """A file row whose chooser saves rather than opens."""

    def _on_clicked(self, _button) -> None:
        dialog = Gtk.FileDialog(title=self.get_title())
        if self.path:
            dialog.set_initial_name(Path(self.path).name)
            parent = Path(self.path).parent
            if parent.exists():
                dialog.set_initial_folder(Gio.File.new_for_path(str(parent)))
        else:
            dialog.set_initial_name("pistorm.img")
        dialog.save(self.get_root(), None, self._finish_save)

    def _finish_save(self, dialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except Exception:  # noqa: BLE001
            return
        if file is not None:
            self.set_path(file.get_path())


def combo(items: list[str], selected: int = 0) -> Gtk.StringList:
    model = Gtk.StringList()
    for item in items:
        model.append(item)
    return model


def _full_text_factory() -> Gtk.SignalListItemFactory:
    """A list factory whose labels are never shortened."""
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, item):
        label = Gtk.Label(xalign=0.0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                          ellipsize=Pango.EllipsizeMode.NONE, max_width_chars=64,
                          margin_top=4, margin_bottom=4)
        item.set_child(label)

    def bind(_factory, item):
        value = item.get_item()
        text = value.get_string() if hasattr(value, "get_string") else str(value)
        item.get_child().set_text(text)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


def show_full_value(*rows) -> None:
    """Make a combo row show its options and its value in full.

    Adw.ComboRow shortens text in two separate places: the selected value sits
    in a narrow slot on the right, and the items in the drop-down are
    ellipsised to the popup's width.  Both hide the end of the string - which
    for a disk description or a screen mode is the part that matters - so both
    have to be dealt with.
    """
    for row in rows:
        row.set_use_subtitle(True)
        row.set_list_factory(_full_text_factory())
