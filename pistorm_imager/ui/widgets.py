"""Small reusable pieces of the interface."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk, Pango  # noqa: E402


class FileRow(Adw.ActionRow):
    """An action row that opens a file (or folder) chooser and shows the choice."""

    def __init__(self, title: str, subtitle: str = "", *, folder: bool = False,
                 filters: list[tuple[str, list[str]]] | None = None,
                 on_change: Callable[[str], None] | None = None):
        super().__init__(title=title, subtitle=subtitle or "None selected")
        self._placeholder = subtitle or "None selected"
        self._folder = folder
        self._filters = filters or []
        self._on_change = on_change
        self.path: str = ""

        self._button = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        self._button.connect("clicked", self._on_clicked)
        self._clear = Gtk.Button(icon_name="edit-clear-symbolic",
                                 valign=Gtk.Align.CENTER, tooltip_text="Clear")
        self._clear.add_css_class("flat")
        self._clear.set_visible(False)
        self._clear.connect("clicked", lambda _b: self.set_path(""))
        self.add_suffix(self._clear)
        self.add_suffix(self._button)
        self.set_activatable_widget(self._button)

    def set_path(self, path: str) -> None:
        self.path = path or ""
        self.set_subtitle(self.path or self._placeholder)
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

    def _finish(self, dialog, result) -> None:
        try:
            file = (dialog.select_folder_finish(result) if self._folder
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
