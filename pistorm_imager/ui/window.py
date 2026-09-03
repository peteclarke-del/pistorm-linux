"""The main PiStorm Imager window."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import __version__  # noqa: E402
from ..core import (amigaos, bootcfg, builder, content, devices,  # noqa: E402
                    distributions,
                    emu68, hdfcheck, jobs, kickstart, machines, packages,
                    prepare, presets, updates)
from ..core.util import (GIB, Progress, describe_size,  # noqa: E402
                         exact_size_text, human_size,  # noqa: E402
                         parse_size)
from .widgets import FileRow, SaveRow, combo, show_full_value  # noqa: E402

SELECT_CARD = "Select a card…"

#  Where the operating system comes from, in the order the combo lists them.
#  Saved sessions record the name rather than the position: inserting an option
#  would otherwise silently change what an old session had chosen.
SYSTEM_SOURCES = ["auto", "pimiga", "image", "adf", "none"]

#  What the card is built around.  These are alternatives, not additions: a
#  drive taken from a hard disk image is not also a PiMiga installation, and
#  offering both at once only ever produced setups that contradicted themselves.
PRIMARY_SOURCES = ["default", "pimiga", "image"]
PRIMARY_LABELS = [
    "Default - build a new drive",
    "PiMiga installation",
    "Amiga hard disk image",
]
#  What "Default" can then put on that drive.
FRESH_SOURCES = ["adf", "none"]

MODES = [
    ("Build a new card", builder.BuildMode.FRESH,
     "Partition the card, install Emu68 and create an empty Amiga drive "
     "ready for HDToolBox or an AmigaOS install."),
    ("Write a pre-built image", builder.BuildMode.IMAGE,
     "Write PiMiga, an Emu68 Hatcher image or a backup of your own card, then "
     "apply your Emu68 build and settings on top."),
    ("Import an Amiga hard disk image", builder.BuildMode.HDF,
     "Take a WinUAE/FS-UAE/HstWB .hdf - the Amiga drive on its own - build the "
     "boot partition around it and write it into the Amiga partition."),
    ("Update an existing card", builder.BuildMode.CUSTOMISE,
     "Leave everything on the card alone and only refresh the boot partition."),
]

FILESYSTEMS = ["PFS3", "PDS3", "FFS-INTL", "FFS", "SFS"]

IMAGE_FILTERS = [
    ("Disk images", ["*.img", "*.IMG", "*.raw", "*.iso", "*.vhd", "*.bin", "*.dd"]),
    ("Compressed images", ["*.xz", "*.gz", "*.bz2", "*.zst", "*.zip", "*.7z", "*.rar"]),
]
ROM_FILTERS = [("Kickstart ROMs", ["*.rom", "*.ROM", "*.bin", "*.a1200"])]
HDF_FILTERS = [("Amiga hard disk images", ["*.hdf", "*.HDF", "*.hdz", "*.rdsk", "*.img"])]
ZIP_FILTERS = [("Emu68 release", ["*.zip"])]


#  The Quick setup page builds a whole configuration from the machine and the
#  card, which is what makes it useful - and what made it destructive: every
#  setting made anywhere else came back at its default, so applying it emptied
#  the WiFi network, the volume name and the boot switches without a word.
#  These are the settings the page has no opinion about, and must hand back.
KEPT_ACROSS_QUICK_SETUP = (
    "release_tag", "emu68_archive", "install_emu68", "kickstart_key",
    "amiga_volume_name", "wifi_ssid", "wifi_password", "wifi_country",
    "expand_to_fill", "extra_partitions",
    #  The quick page has a source chooser of its own, so these belong to the
    #  Source page alone: applying a fresh layout used to empty it.
    "source_image", "hdf_image", "repair_rdb",
)

#  The same for the boot settings.  The machine decides the ones that follow
#  from its chipset and its display; these are the ones only a person can.
KEPT_BOOT_OPTIONS = (
    "overclock", "cm4_external_antenna", "swap_df0_with_df1", "sd_unit0_rw",
    "hdmi_force_hotplug", "boot_delay", "gpu_mem", "total_mem", "limit_2g",
    "z2_ram_size", "unicam_extra",
)


def merge_cmdline(from_machine: str, typed: str) -> str:
    """Both sets of extra cmdline options, without repeating any.

    The machine's own options and whatever was typed by hand share a single
    field, so one of the two used to be thrown away.  The machine's words are
    dropped from the typed side before the two are joined: they are put back
    when they still apply, and must not linger once the switch that added them
    is turned off.
    """
    words = from_machine.split()
    words += [word for word in typed.split()
              if word not in words and word not in machines.CMDLINE_OPTIONS]
    return " ".join(words)


#  What each quick-start screen shows, in the order it shows it.  Reading down
#  a screen should follow the order of the decisions: what the card is for,
#  what goes on it, where it is going, and finally what that adds up to.
QUICK_SCREENS = {
    "choices": ("group_choices",),
    "basic": ("group_hardware", "group_detected", "group_target", "group_plan"),
    "prepared": ("image_group", "group_target", "group_plan"),
}

FIRST_DRIVE = "The first bootable drive"
NO_IMAGE = "Choose an image first"
#  An image can be chosen and still have nothing to offer.  Saying "choose an
#  image first" then reads as if the choice had not registered at all.
NO_DRIVES = "No Amiga drive could be read from this image"


class PartitionRow(Adw.ExpanderRow):
    """Editor for one Amiga partition inside the RDB."""

    def __init__(self, spec: builder.AmigaPartitionSpec, on_remove, on_change,
                 machine=None):
        super().__init__()
        self._on_change = on_change
        #  What the card is for decides which categories are worth copying, so
        #  the row asks rather than being told once at construction.
        self._machine = machine or (lambda: machines.MACHINES[0])
        #  Everything the editor does not show - where the contents come from,
        #  what to leave out, files to overlay - has to survive being edited.
        #  Rebuilding the spec from the widgets alone silently discarded it.
        self._source = spec

        self.name_row = Adw.EntryRow(title="Device name (DH0, DH1, ...)")
        self.name_row.set_text(spec.name)
        self.volume_row = Adw.EntryRow(title="Volume name, as Workbench shows it")
        self.volume_row.set_text(spec.volume_name or "")
        self.size_row = Adw.EntryRow(title="Size (e.g. 2G, 512M, or 'rest')")
        self.size_row.set_text("rest" if spec.size is None else human_size(spec.size)
                               .replace(" GiB", "G").replace(" MiB", "M"))
        self.fs_row = Adw.ComboRow(title="File system", model=combo(FILESYSTEMS))
        if spec.dostype in FILESYSTEMS:
            self.fs_row.set_selected(FILESYSTEMS.index(spec.dostype))
        self.boot_row = Adw.SwitchRow(
            title="Bootable", subtitle="Mark this partition as the boot drive")
        self.boot_row.set_active(spec.bootable)
        self.priority_row = Adw.SpinRow.new_with_range(-128, 127, 1)
        self.priority_row.set_title("Boot priority")
        self.priority_row.set_subtitle("Higher boots first; 0 is the usual "
                                       "value for a system drive")
        self.priority_row.set_value(spec.boot_priority)
        self.content_row = Adw.ActionRow(title="Contents")
        self.content_row.set_sensitive(False)
        #  A partition can be filled from an image of its own, so a drive out
        #  of an .hdf can be added alongside another source rather than
        #  replacing it.
        self.hdf_row = FileRow(
            "Fill this partition from",
            "A hard disk image to take a drive out of, or a folder of files "
            "to copy in - PiMiga's Games and Demos drives are folders",
            both=True, filters=HDF_FILTERS,
            on_change=lambda _p: self._on_hdf_chosen())
        #  Which drive to take is a choice between the ones the image actually
        #  holds, named as Workbench names them - not a device name typed from
        #  memory and silently wrong.
        #  Categories found in whatever this partition is filled from, each
        #  a switch.  Built when a folder is chosen, because until then there
        #  is nothing to divide up.
        self.exclude_group = Adw.ExpanderRow(
            title="Leave out", subtitle="Choose a folder to see what it holds")
        self._category_rows: dict[str, Adw.SwitchRow] = {}
        #  Anything already excluded that the tree does not explain is kept
        #  rather than quietly dropped.
        self._extra_excludes: list[str] = list(spec.exclude or ())
        self.hdf_part_row = Adw.ComboRow(title="Which drive to import",
                                         model=combo([FIRST_DRIVE]))
        self._drive_keys: list[str] = [""]
        #  Filling these in fires the callbacks, so both rows exist first.
        self.hdf_row.set_path(spec.content_hdf or "")
        self._reload_drives(spec.content_hdf_partition or "")
        self.hdf_part_row.connect("notify::selected", lambda *_a: self._refresh())

        for row in (self.name_row, self.volume_row, self.size_row, self.fs_row,
                    self.boot_row, self.priority_row, self.content_row,
                    self.hdf_row, self.hdf_part_row, self.exclude_group):
            self.add_row(row)
        for row in (self.name_row, self.volume_row, self.size_row):
            row.connect("changed", lambda _r: self._refresh())
        self.fs_row.connect("notify::selected", lambda *_a: self._refresh())
        self.boot_row.connect("notify::active", lambda *_a: self._refresh())
        self.priority_row.connect("notify::value", lambda *_a: self._refresh())

        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Remove this partition")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda _b: on_remove(self))
        self.add_suffix(remove)
        self._refresh()

    def _describe_contents(self, spec: builder.AmigaPartitionSpec) -> str:
        if spec.content_folder:
            text = f"copied from {Path(spec.content_folder).name}"
        elif spec.content_hdf:
            where = (f" partition {spec.content_hdf_partition}"
                     if spec.content_hdf_partition else "")
            text = f"copied from {Path(spec.content_hdf).name}{where}"
        elif spec.bootable:
            text = "whatever the operating system choice installs"
        else:
            text = "left empty - format it on the Amiga"
        if spec.exclude:
            text += f"; leaving out {', '.join(spec.exclude)}"
        if spec.overlays:
            text += f"; plus {len(spec.overlays)} extra item(s)"
        return text

    def _on_hdf_chosen(self) -> None:
        self._reload_drives(self._source.content_hdf_partition)
        self.reload_categories()
        self._refresh()

    def _excluded(self) -> list[str]:
        """Category paths switched off, plus anything we could not explain."""
        chosen = [path for path, row in self._category_rows.items()
                  if row.get_active()]
        return chosen + [p for p in self._extra_excludes
                         if p not in self._category_rows]

    def reload_categories(self) -> None:
        """List what the chosen folder holds, defaulting to what runs here.

        A category the machine cannot use starts switched off - the AGA games
        on an A500 - but every one stays changeable, because "cannot run it"
        is a sensible default and not a rule.
        """
        for row in self._category_rows.values():
            self.exclude_group.remove(row)
        self._category_rows.clear()

        path = self.hdf_row.path
        folder = bool(path) and Path(path).is_dir()
        found = content.discover(path) if folder else []
        if not folder:
            self.exclude_group.set_subtitle("Choose a folder to see what it holds")
        elif not found:
            self.exclude_group.set_subtitle("Nothing in here is divided into "
                                            "categories")
        else:
            machine = self._machine()
            unsuitable = set(content.unsuitable(found, machine))
            already = set(self._extra_excludes)
            for category in found:
                #  A choice already made wins over the default.
                off = (category.path in already if already
                       else category.path in unsuitable)
                note = category.note or "No hardware requirement known"
                row = Adw.SwitchRow(
                    title=f"{category.label}  ({category.entries})",
                    subtitle=note + ("" if category.suits(machine)
                                     else f"  -  not for the {machine.label}"))
                row.set_active(off)
                row.connect("notify::active", lambda *_a: self._refresh())
                self._category_rows[category.path] = row
                self.exclude_group.add_row(row)
            self.exclude_group.set_subtitle(
                f"{len(found)} categories, {len(unsuitable)} of them not for "
                f"this machine")

    def choose_drive(self, name: str) -> bool:
        """Select a drive by device name; False if the image has no such drive."""
        wanted = (name or "").strip().upper()
        keys = [k.upper() for k in self._drive_keys]
        if wanted and wanted in keys:
            self.hdf_part_row.set_selected(keys.index(wanted))
            return True
        self.hdf_part_row.set_selected(0)
        return not wanted

    def _chosen_drive(self) -> str:
        index = self.hdf_part_row.get_selected()
        if 0 <= index < len(self._drive_keys):
            return self._drive_keys[index]
        return ""

    def _reload_drives(self, keep: str = "") -> None:
        """List the drives in the chosen image, so one can be picked by name."""
        path = self.hdf_row.path
        folder = bool(path) and Path(path).is_dir()
        drives = builder.list_drives(path) if path and not folder else []
        self._drive_keys = [""]
        if folder:
            #  A folder is copied in whole; there are no drives to choose.
            labels = [f"Everything in {Path(path).name}"]
        elif not path:
            labels = [NO_IMAGE]
        elif not drives:
            labels = [NO_DRIVES]
        elif len(drives) == 1 and drives[0].whole_image:
            #  A bare file system with no partition table: there is nothing to
            #  choose between, so say what it is rather than offer a choice.
            labels = [drives[0].label]
        else:
            labels = [FIRST_DRIVE]
            for drive in drives:
                labels.append(drive.label)
                self._drive_keys.append(drive.name)
        self.hdf_part_row.set_model(combo(labels))
        self.hdf_part_row.set_sensitive(len(labels) > 1)
        if not path:
            self.hdf_part_row.set_subtitle("Choose a file or folder above")
        elif drives:
            self.hdf_part_row.set_subtitle("")
        elif folder:
            self.hdf_part_row.set_subtitle("")
        else:
            #  Say what the file actually is.  "No Amiga drive found" is true
            #  of a PiMiga download and tells the user nothing they can act on.
            self.hdf_part_row.set_subtitle(builder.why_no_drives(path))
        wanted = (keep or "").strip().upper()
        if wanted in [k.upper() for k in self._drive_keys[1:]]:
            self.hdf_part_row.set_selected(
                [k.upper() for k in self._drive_keys].index(wanted))
        else:
            self.hdf_part_row.set_selected(0)

    def _refresh(self) -> None:
        spec = self.spec()
        size = "remaining space" if spec.size is None else human_size(spec.size)
        label = spec.volume_name or spec.name
        self.set_title(f"{spec.name} ({label}:)" if spec.volume_name else
                       (spec.name or "(unnamed)"))
        self.set_subtitle(f"{size} · {spec.dostype}"
                          + (f" · bootable, priority {spec.boot_priority}"
                             if spec.bootable else ""))
        self.content_row.set_subtitle(self._describe_contents(spec))
        self.priority_row.set_visible(spec.bootable)
        self.hdf_part_row.set_visible(bool(self.hdf_row.path))
        if self._on_change:
            self._on_change()

    def spec(self) -> builder.AmigaPartitionSpec:
        text = self.size_row.get_text().strip().lower()
        if text in ("", "rest", "remaining", "all", "max"):
            size = None
        else:
            try:
                size = parse_size(text)
            except ValueError:
                size = None
        #  Override only what this editor shows; keep the rest of the spec.
        chosen = self.hdf_row.path
        is_folder = bool(chosen) and Path(chosen).is_dir()
        return dataclasses.replace(
            self._source,
            name=self.name_row.get_text().strip().upper() or "DH0",
            volume_name=self.volume_row.get_text().strip(),
            size=size,
            dostype=FILESYSTEMS[self.fs_row.get_selected()],
            bootable=self.boot_row.get_active(),
            boot_priority=int(self.priority_row.get_value()),
            #  An image chosen here replaces whatever the partition was going
            #  to be filled with, rather than fighting with it.
            content_hdf=(chosen if chosen and not is_folder
                         else "" if chosen
                         else self._source.content_hdf),
            content_hdf_partition=(self._chosen_drive() if chosen and not is_folder
                                   else "" if chosen
                                   else self._source.content_hdf_partition),
            content_folder=(chosen if chosen and is_folder
                            else "" if chosen
                            else self._source.content_folder),
            exclude=self._excluded(),
        )


class ImagerWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application, title="PiStorm Imager",
                         default_width=880, default_height=760)
        #  Widgets on later pages do not exist while earlier pages are being
        #  built, and building a page can fire change callbacks.  Nothing reads
        #  the widget state until construction has finished.
        self._ready = False
        self.releases: list[emu68.Release] = []
        self.device_list: list[devices.Device] = []
        self.worker: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.cancel_flag = threading.Event()

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.outer = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.outer)
        self.outer.add_named(self._build_setup(), "setup")
        self.outer.add_named(self._build_progress(), "progress")

        #  Long values - a disk description, a screen mode, a board name - are
        #  ellipsised in a combo row's value slot; show them in full instead.
        show_full_value(
            self.mode_row, self.variant_row, self.release_row,
            self.quick_machine, self.quick_display, self.quick_system_source,
            self.quick_primary,
            self.quick_target, self.quick_device, self.hdmi_row,
            self.overclock_row, self.antenna_row, self.target_row,
            self.device_row, self.os_version_row,
        )
        self._ready = True
        self._refresh_packages()
        self._mirror_target()
        self._on_machine_changed()
        self._detect_material()
        self._refresh_devices()
        self._restore_session()
        self.connect("close-request", self._on_close)
        self._load_releases_async()
        self._sync_visibility()
        #  Start on the quick start with nothing else in the way.  A restored
        #  session that was in the middle of customising reopens there.
        self._set_customising(getattr(self, "_restored_customising", False))

    # ------------------------------------------------------------ setup UI

    def _build_setup(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        #  A real menu model rather than a popover full of buttons: this is
        #  what closes itself when an item is chosen, and it brings keyboard
        #  navigation and the platform's own styling with it.
        entries = (("Save settings…", "save-settings", self._on_save_settings),
                   ("Load settings…", "load-settings", self._on_load_settings),
                   ("Forget saved setup", "forget-session", self._on_forget_session),
                   ("Inspect the target", "inspect-target", self._on_inspect),
                   ("Check for updates…", "check-updates", self._on_check_updates),
                   ("About", "about", self._on_about))
        model = Gio.Menu()
        for label, name, handler in entries:
            action = Gio.SimpleAction.new(name, None)
            #  The handlers predate this and take a widget they do not use.
            action.connect("activate", lambda _a, _p, run=handler: run(None))
            self.add_action(action)
            model.append(label, f"win.{name}")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic",
                                       menu_model=model,
                                       tooltip_text="Menu"))
        view.add_top_bar(header)

        self.stack.add_titled_with_icon(self._page_quick(), "quick", "Quick setup",
                                        "starred-symbolic")
        self.stack.add_titled_with_icon(self._page_source(), "source", "Source",
                                        "folder-download-symbolic")
        amiga_page = self._page_amiga()
        self.stack.add_titled_with_icon(self._page_storage(), "storage", "Storage",
                                        "drive-harddisk-symbolic")
        #  Everything past the quick start is the customising workflow, and is
        #  hidden until it is asked for.
        self.stack.add_titled_with_icon(amiga_page, "amiga", "Amiga",
                                        "applications-system-symbolic")
        self.stack.add_titled_with_icon(self._page_options(), "options", "Options",
                                        "preferences-system-symbolic")
        self.stack.add_titled_with_icon(self._page_target(), "target", "Target",
                                        "media-flash-symbolic")
        view.set_content(self.stack)

        #  The quick start is a choice of three things to do, not a page among
        #  equals: it is all there is until "Customise" is chosen, and this
        #  button is how to get back to it afterwards.
        self._customising = False

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                         margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        #  Back sits with Write, at the other end of the same bar: they are
        #  the two things you do when you have finished reading the page.
        self.back_button = Gtk.Button(label="Back")
        self.back_button.add_css_class("pill")
        self.back_button.connect("clicked", lambda _b: self._go_back())
        bottom.append(self.back_button)
        self.summary = Gtk.Label(xalign=0.0, wrap=True, hexpand=True)
        self.summary.add_css_class("dim-label")
        bottom.append(self.summary)
        self.write_button = Gtk.Button(label="Write card")
        self.write_button.add_css_class("suggested-action")
        self.write_button.add_css_class("pill")
        self.write_button.connect("clicked", self._on_write)
        bottom.append(self.write_button)
        self.bottom_bar = bottom
        view.add_bottom_bar(bottom)
        return view

    def _go_back(self) -> None:
        """Back to the choice, from wherever going back makes sense.

        Always to the choice itself, not to whichever screen was last open:
        coming out of the workflow onto the basic-card screen looks like the
        first screen with a Back button on it, which is not a place that
        exists.

        Going back also withdraws the setup.  It had been accepted, and Write
        stayed lit while the choice that led to it was being reconsidered -
        which is the one moment it should not be.
        """
        self._applied_config = None
        self._quick_screen = "choices"
        if getattr(self, "_customising", False):
            self._set_customising(False)
        else:
            self._set_quick_screen("choices")

    def _update_back(self) -> None:
        """Back is only shown where there is somewhere to go.

        The whole bar goes with it on the first screen: nothing has been
        chosen yet, so there is nothing to summarise, nothing to go back to
        and nothing to write.  A choice is all that screen is.
        """
        if not hasattr(self, "back_button"):
            return
        beyond_the_choice = (getattr(self, "_customising", False)
                             or getattr(self, "_quick_screen", "choices")
                             != "choices")
        self.back_button.set_visible(beyond_the_choice)
        if hasattr(self, "bottom_bar"):
            self.bottom_bar.set_visible(beyond_the_choice)

    def _move_group(self, group, page) -> None:
        """Put a group on a page, taking it off whatever page it is on.

        Two screens genuinely need the same settings - the Amiga model matters
        to a basic card and to a customised one - and a widget has one parent,
        so it is moved rather than duplicated.  Duplicating would mean two
        controls for one setting, which is worse than either.
        """
        current = group.get_ancestor(Adw.PreferencesPage)
        if current is page:
            return
        if current is not None:
            current.remove(group)
        page.add(group)

    @staticmethod
    def _move_row(row, group) -> None:
        """Put one row in ``group``, taking it off wherever it was.

        The same idea as _move_group, for the choosers the quick start needs
        to borrow: the Kickstart and the Workbench disks are chosen on the
        Amiga and Source pages, and a quick screen that shows neither still
        has to let someone say where they are.
        """
        current = row.get_ancestor(Adw.PreferencesGroup)
        if current is group:
            return
        if current is not None:
            current.remove(row)
        group.add(row)

    def _set_quick_screen(self, name: str) -> None:
        """Which of the quick start's three screens is showing.

        The first is the choice and nothing else: a page of settings under it
        is not a choice, it is the thing being chosen between.
        """
        self._quick_screen = name
        wanted = QUICK_SCREENS.get(name, QUICK_SCREENS["choices"])

        #  Everything the quick start can show, taken off the page so it can
        #  go back on in the order this screen wants.  add() appends, so a
        #  group moved here from another page landed last - which is how the
        #  image chooser ended up underneath the summary that describes it.
        movable = ("group_choices", "group_hardware", "group_detected",
                   "image_group", "group_target", "group_plan")
        for attribute in movable:
            group = getattr(self, attribute, None)
            if group is not None and group.get_ancestor(Adw.PreferencesPage) \
                    is self.page_quick:
                self.page_quick.remove(group)
        for attribute in wanted:
            group = getattr(self, attribute)
            self._move_group(group, self.page_quick)
            group.set_visible(True)

        #  The quick start told people what it had found to install from and
        #  gave them no way to correct it - the choosers live on the Amiga and
        #  Source pages, which a quick screen does not show.  So a card built
        #  from floppies could not be pointed at the floppies.
        if name == "basic":
            self._move_row(self.rom_row, self.group_detected)
            self._move_row(self.quick_system_source, self.group_detected)
            self._move_row(self.adf_row, self.group_detected)
        else:
            self._move_row(self.rom_row, self.group_kickstart)
            self._move_row(self.quick_system_source, self.group_primary)
            self._move_row(self.adf_row, self.os_group)

        #  What this screen does not want goes home, so the workflow finds it
        #  where it belongs rather than missing.
        if "group_hardware" not in wanted:
            self._move_group(self.group_hardware, self.page_amiga)
            self.group_hardware.set_visible(True)
        if "image_group" not in wanted:
            self._move_group(self.image_group, self.page_source)
        for attribute in movable:
            if attribute not in wanted and attribute in ("group_choices",
                                                         "group_detected",
                                                         "group_target",
                                                         "group_plan"):
                getattr(self, attribute).set_visible(False)
        self._update_back()
        self._update_summary()

    def _choose_basic(self) -> None:
        """Emu68 and an empty Amiga drive, ready for a floppy install."""
        self.quick_primary.set_selected(PRIMARY_SOURCES.index("default"))
        #  If Workbench disks have been found, install them: a card with an
        #  empty drive is not what most people mean by a basic PiStorm card,
        #  and the choice can still be changed on the screen itself.
        detected = getattr(self, "detected", None)
        wants = "adf" if (detected and detected.adf_folder) else "none"
        self.quick_system_source.set_selected(FRESH_SOURCES.index(wants))
        self.image_row.set_path("")
        self.quick_hdf.set_path("")
        self.quick_pimiga.set_path("")
        self.mode_row.set_selected(0)            # a fresh card
        self._on_source_changed()
        self._applied_config = None
        self._set_quick_screen("basic")

    def _choose_prepared(self) -> None:
        """Write a finished system somebody else built."""
        self.quick_primary.set_selected(PRIMARY_SOURCES.index("image"))
        for index, mode in enumerate(MODES):
            if "image" in mode[0].lower():
                self.mode_row.set_selected(index)
                break
        self._on_source_changed()
        self._applied_config = None
        self._set_quick_screen("prepared")

    def _set_customising(self, on: bool) -> None:
        """Switch between the quick start and the full workflow.

        The quick start is not a page among equals - it is the whole window
        until someone asks for more - so the others are hidden rather than
        merely unselected, and the switcher has nothing to offer but the one
        thing there is to do.
        """
        self._customising = bool(on)
        for name in ("source", "storage", "amiga", "options", "target"):
            page = self.stack.get_page(self.stack.get_child_by_name(name))
            if page is not None:
                page.set_visible(self._customising)
        quick = self.stack.get_page(self.stack.get_child_by_name("quick"))
        if quick is not None:
            quick.set_visible(not self._customising)
        self._update_back()
        if self._customising:
            #  The full workflow owns these again.
            self._move_group(self.group_hardware, self.page_amiga)
            self._move_group(self.image_group, self.page_source)
            self.group_hardware.set_visible(True)
            #  And the choosers the quick start borrowed, or the Amiga and
            #  Source pages come up without a way to pick a Kickstart or the
            #  Workbench disks at all.
            self._move_row(self.rom_row, self.group_kickstart)
            self._move_row(self.quick_system_source, self.group_primary)
            self._move_row(self.adf_row, self.os_group)
            #  Finishing happens on Target, so that is where the summary and
            #  the button that accepts it belong.
            self._move_group(self.group_plan, self.page_target)
            self.group_plan.set_visible(True)
        else:
            self._set_quick_screen(getattr(self, "_quick_screen", "choices"))
        self.stack.set_visible_child_name("source" if self._customising
                                          else "quick")

    def _page_quick(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        self.page_quick = page


        #  Three things anyone actually wants to do, rather than a page of
        #  settings that happens to be first.
        choices = Adw.PreferencesGroup(
            title="What would you like to do?",
            #  Nothing is below it any more - this screen is the choice
            #  and nothing else - so it can no longer promise settings here.
            description="Each one leads to what it needs, and back here if "
                        "you change your mind.")
        for title, subtitle, label, handler in (
            ("A basic PiStorm card",
             "Emu68 and an empty Amiga drive, partitioned and formatted, ready "
             "to install Workbench onto from floppies.",
             "Set up", self._choose_basic),
            ("Write a prepared system",
             "A finished image you have downloaded - CaffeineOS, an Emu68 "
             "Hatcher image, or a backup of a card.",
             "Choose image", self._choose_prepared),
            ("Customise an installation",
             "The full workflow: sources, storage, the software to add, boot "
             "options. Everything the other two decide for you.",
             "Customise", lambda: self._set_customising(True)),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            button.add_css_class("suggested-action")
            button.connect("clicked", lambda _b, h=handler: h())
            row.add_suffix(button)
            row.set_activatable_widget(button)
            choices.add(row)
        self.group_choices = choices
        page.add(choices)

        group = Adw.PreferencesGroup(
            title="Quick setup",
            description="Builds the layout that suits a PiStorm card: a small "
                        "FFS system drive, which Kickstart can mount with no "
                        "driver at all, and a PFS3 work drive for the rest of "
                        "the card, because FFS on tens of gigabytes is slow and "
                        "needs a full validation pass after every unclean "
                        "shutdown.")
        self.quick_found_rom = Adw.ActionRow(title="Kickstart", subtitle="Looking…")
        self.quick_found_rom.set_sensitive(False)
        group.add(self.quick_found_rom)
        self.quick_found_adf = Adw.ActionRow(title="Workbench disks", subtitle="Looking…")
        self.quick_found_adf.set_sensitive(False)
        group.add(self.quick_found_adf)
        rescan = Gtk.Button(icon_name="view-refresh-symbolic",
                            valign=Gtk.Align.CENTER, tooltip_text="Look again")
        rescan.add_css_class("flat")
        rescan.connect("clicked", lambda _b: self._detect_material())
        group.set_header_suffix(rescan)
        self.group_detected = group
        page.add(group)

        group = Adw.PreferencesGroup(
            title="Your hardware",
            description="Almost everything on the card is the same whatever "
                        "Amiga it goes into. The model decides the PiStorm "
                        "board, the Kickstart, the display settings, and which "
                        "chipset-specific games are worth copying.")
        self.quick_machine = Adw.ComboRow(
            title="Amiga model",
            model=combo([m.label for m in machines.MACHINES]))
        self.quick_machine.connect("notify::selected",
                                   lambda *_a: self._on_machine_changed())
        group.add(self.quick_machine)
        self.quick_machine_hint = Adw.ActionRow(title="", subtitle="")
        self.quick_machine_hint.set_sensitive(False)
        group.add(self.quick_machine_hint)
        self.quick_display = Adw.ComboRow(
            title="How you look at it",
            model=combo([d.label for d in machines.Display]))
        self.quick_display.connect("notify::selected",
                                   lambda *_a: self._on_display_changed())
        group.add(self.quick_display)
        #  Only a setup with both outputs has anything to decide here; with one
        #  output the answer is forced and the row is hidden.
        self.quick_workbench_screen = Adw.ComboRow(
            title="Workbench opens on, to start with",
            subtitle="Both drivers are installed either way. Switch on the "
                     "Amiga with Execute S:PiStorm-Use-HDMI or Execute "
                     "S:PiStorm-Use-Amiga-Video, then reboot.",
            model=combo(["The RTG screen on the Pi's HDMI",
                         "A native screen on the Amiga's own video output"]))
        self.quick_workbench_screen.connect(
            "notify::selected", lambda *_a: self._on_layout_changed())
        group.add(self.quick_workbench_screen)
        self.quick_trapdoor = Adw.SwitchRow(
            title="Trapdoor 512K fitted, use it as chip RAM",
            subtitle="A500 and A500+ only")
        group.add(self.quick_trapdoor)
        #  What the machine is belongs with the machine; the Amiga
        #  page adds this.
        self.group_hardware = group

        group = Adw.PreferencesGroup(
            title="Primary installation",
            description="Where the Amiga system and everything on the card "
                        "comes from.  These are alternatives: a drive taken "
                        "from a hard disk image is not also a PiMiga "
                        "installation.")
        self.quick_primary = Adw.ComboRow(title="Build the card around",
                                          model=combo(PRIMARY_LABELS))
        self.quick_primary.connect("notify::selected",
                                   lambda *_a: self._on_primary_changed())
        group.add(self.quick_primary)
        self.quick_pimiga = FileRow(
            "PiMiga folder",
            "Its drives, games and demos are copied over, and its graphics "
            "driver replaced.  Collections needing a chipset this machine "
            "does not have are left out.",
            folder=True, on_change=lambda _p: self._on_source_changed())
        group.add(self.quick_pimiga)
        self.quick_pimiga_info = Adw.ActionRow(title="Content",
                                               subtitle="No folder selected")
        self.quick_pimiga_info.set_sensitive(False)
        group.add(self.quick_pimiga_info)
        self.quick_hdf = FileRow(
            "Amiga hard disk image",
            "Its partition scheme is copied onto the card, and its graphics "
            "driver adapted", filters=HDF_FILTERS,
            on_change=lambda _p: self._on_quick_hdf())
        group.add(self.quick_hdf)
        self.quick_hdf_info = Adw.ActionRow(title="Scheme",
                                            subtitle="No image selected")
        self.quick_hdf_info.set_sensitive(False)
        group.add(self.quick_hdf_info)
        self.quick_system_source = Adw.ComboRow(
            title="Operating system",
            model=combo(["Install Workbench from my floppy images",
                         "Don't install one - partition only"]))
        self.quick_system_source.connect("notify::selected",
                                         lambda *_a: self._on_source_changed())
        group.add(self.quick_system_source)
        self.quick_os_hint = Adw.ActionRow(
            title="",
            subtitle="A Workbench installed from floppies is small and uses "
                     "native screen modes; PiMiga's system is ready made but "
                     "built around RTG.")
        self.quick_os_hint.set_sensitive(False)
        group.add(self.quick_os_hint)
        #  Where the system comes from belongs with the other
        #  sources; the Source page adds this.
        self.group_primary = group

        group = Adw.PreferencesGroup(title="Choices")
        self.quick_system = Adw.EntryRow(title="System drive size")
        self.quick_system.set_text("1G")
        self.quick_system.connect("changed", lambda _r: self._on_layout_changed())
        group.add(self.quick_system)
        self.quick_work = Adw.SwitchRow(
            title="Add a PFS3 work drive",
            subtitle="Takes the rest of the card; format it on the Amiga. Not "
                     "used when the layout comes from PiMiga or an image.")
        self.quick_work.set_active(True)
        self.quick_work.connect("notify::active",
                                lambda *_a: self._on_layout_changed())
        group.add(self.quick_work)
        self.quick_donor = FileRow(
            "PFS3 handler",
            "Looking for one…", filters=HDF_FILTERS,
            on_change=lambda _p: self._quick_preview())
        group.add(self.quick_donor)
        #  Sizes are a storage question; the Storage page adds this.
        self.group_sizes = group

        group = Adw.PreferencesGroup(
            title="Where to write it",
            description="A card is written directly; an image file is sparse, "
                        "costs nothing to make, and can be inspected before you "
                        "commit it to a card.")
        self.quick_target = Adw.ComboRow(
            title="Write to", model=combo(["SD card", "SD card image file"]))
        self.quick_target.connect("notify::selected",
                                  lambda *_a: self._mirror_target())
        group.add(self.quick_target)
        self.quick_device = Adw.ComboRow(title="Card", model=combo([SELECT_CARD]))
        self.quick_device.connect("notify::selected",
                                  lambda *_a: self._mirror_target())
        group.add(self.quick_device)
        rescan_cards = Gtk.Button(icon_name="view-refresh-symbolic",
                                  valign=Gtk.Align.CENTER,
                                  tooltip_text="Rescan for cards")
        rescan_cards.add_css_class("flat")
        rescan_cards.connect("clicked", lambda _b: self._refresh_devices())
        group.set_header_suffix(rescan_cards)
        self.quick_file = SaveRow("Save image as", filters=IMAGE_FILTERS,
                                  on_change=lambda _p: self._mirror_target())
        group.add(self.quick_file)
        self.quick_card_size = Adw.EntryRow(
            title="Card or image size - 32GB as cards are sold, 32GiB binary")
        self.quick_card_size.set_text("32GB")
        self.quick_card_size.connect("changed", lambda _r: self._mirror_target())
        group.add(self.quick_card_size)
        self.quick_size_info = Adw.ActionRow(title="Size", subtitle="")
        self.quick_size_info.set_sensitive(False)
        group.add(self.quick_size_info)
        self.group_target = group
        page.add(group)

        #  The same block wherever the setup is finished: what it adds up to,
        #  and the button that accepts it, at the bottom of the last thing
        #  read.  It moves to the Target page when customising.
        group = Adw.PreferencesGroup(
            title="What this will build",
            description="Everything chosen so far, and what it comes to.")
        self.quick_plan = Gtk.Label(xalign=0.0, wrap=True, selectable=True,
                                    margin_top=6, margin_bottom=6,
                                    margin_start=12, margin_end=12)
        self.quick_plan.add_css_class("dim-label")

        #  All one box.  A preferences group keeps plain widgets and rows in
        #  separate places, so adding the summary and then an ActionRow does
        #  not put the row after the summary - it puts it wherever the group
        #  keeps rows, which was above it.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("card")
        box.append(self.quick_plan)

        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                        margin_top=6, margin_bottom=12,
                        margin_start=12, margin_end=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        heading = Gtk.Label(xalign=0.0, label="Apply this setup")
        self.apply_note = Gtk.Label(xalign=0.0, wrap=True)
        self.apply_note.add_css_class("dim-label")
        self.apply_note.add_css_class("caption")
        titles.append(heading)
        titles.append(self.apply_note)
        strip.append(titles)
        self.apply_button = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self._on_apply_quick)
        strip.append(self.apply_button)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        box.append(strip)
        group.add(box)
        #  Kept under the old name so callers still have something to ask.
        self.apply_row = strip
        self.group_plan = group
        page.add(group)
        return page

    def _imported_needs_floppies(self) -> bool:
        """Whether the drive being imported brings no Workbench of its own."""
        path = self.quick_hdf.path
        if not path:
            return False
        try:
            return presets.inspect_image_system(path).needs_floppies
        except Exception:                        # noqa: BLE001 - not fatal
            return False

    def _on_quick_hdf(self) -> None:
        path = self.quick_hdf.path
        if not path:
            self.quick_hdf_info.set_subtitle("No image selected")
            self._relayout_partitions()
            self._quick_preview()
            return
        scheme = presets.describe_image_scheme(path)
        system = presets.inspect_image_system(path)
        text = f"{scheme}. Contains {system.describe()}"
        if system.needs_floppies:
            text += ("  -  choose \u201cinstall Workbench from my floppy "
                     "images\u201d as well, or the card will not boot.")
        #  A ready-made drive built for an A1200 says so only by the display
        #  modes it installs. This check was written to say that and then
        #  never called, so nobody was ever warned.
        for warning in presets.check_image_for_machine(path, self._machine()):
            text += f"  -  {warning}"
        self.quick_hdf_info.set_subtitle(GLib.markup_escape_text(text))
        self._relayout_partitions()
        self._quick_preview()

    def _primary(self) -> str:
        """What the card is being built around."""
        return PRIMARY_SOURCES[self.quick_primary.get_selected()]

    def _system_source(self) -> str:
        """Where the operating system comes from, given the primary choice."""
        primary = self._primary()
        if primary == "pimiga":
            return "pimiga" if self.quick_pimiga.path else "none"
        if primary == "image":
            return "image" if self.quick_hdf.path else "none"
        return FRESH_SOURCES[self.quick_system_source.get_selected()]

    def _on_primary_changed(self) -> None:
        """Switching sources drops the one being left behind.

        Leaving a stale path behind would carry it into the build - a PiMiga
        folder still filling the card after a hard disk image was chosen to
        replace it - so whichever source is no longer primary is cleared.
        """
        primary = self._primary()
        if primary != "pimiga":
            self.quick_pimiga.set_path("")
        if primary != "image":
            self.quick_hdf.set_path("")
        self._sync_visibility()
        self._on_quick_hdf()

    def _mirror_target(self) -> None:
        """Copy the Quick setup target onto the Target page, which is canonical.

        Quick setup has to stand on its own - having to visit another tab to say
        where the card goes defeats the point of it - but there must still be
        only one source of truth for the build.
        """
        if getattr(self, "_mirroring", False) or not self._ready:
            return
        self._mirroring = True
        try:
            self.target_row.set_selected(self.quick_target.get_selected())
            self.device_row.set_selected(self.quick_device.get_selected())
            if self.quick_file.path:
                self.file_row.set_path(self.quick_file.path)
            self.file_size_row.set_text(self.quick_card_size.get_text())
            self.quick_device.set_visible(self.quick_target.get_selected() == 0)
            self.quick_file.set_visible(self.quick_target.get_selected() == 1)
            self._follow_the_card()
            self._show_size()
        finally:
            self._mirroring = False
        self._sync_visibility()
        self._relayout_partitions()

    def _follow_the_card(self) -> None:
        """Show the card's own size when writing to one, and lock the box.

        A size typed for a card is a guess at what the card holds, and the two
        meanings of "GB" make it a bad one: "125G" is 125 GiB, nine gigabytes
        more than a card sold as 125 GB. When there is a card in front of us
        its capacity is known exactly, so it is shown and the box is closed.
        """
        card = self._selected_device()
        for row in (self.quick_card_size, self.file_size_row):
            if card is not None and card.size:
                if row.get_text() != human_size(card.size):
                    row.set_text(human_size(card.size))
                row.set_sensitive(False)
            else:
                row.set_sensitive(True)
        if card is not None and card.size:
            self.quick_card_size.set_title(
                f"Card size - taken from {card.name}, which holds "
                f"{describe_size(card.size)}")
        else:
            self.quick_card_size.set_title(
                "Card or image size - 32GB as cards are sold, 32GiB binary")

    def _extra_cmdline(self) -> str:
        """The cmdline options: what was typed, plus what the switches decide.

        The trapdoor switch owns ``move_slow_to_chip``. It used to reach the
        box only when the quick setup was applied, so a setup loaded with the
        switch on and the option missing built a card without it - 512K of
        chip RAM on a machine told to give it a megabyte - while the switch on
        screen still said it was on. Asking the switch here means the two
        cannot disagree.
        """
        machine = self._machine()
        owned = ("move_slow_to_chip"
                 if machine.trapdoor_ram and self.quick_trapdoor.get_active()
                 else "")
        return merge_cmdline(owned, self.extra_row.get_text().strip()).strip()

    def _boot_size(self) -> int:
        """The boot partition size, as typed on the Target page."""
        try:
            return parse_size(self.boot_size_row.get_text())
        except ValueError:
            return presets.DEFAULT_BOOT_SIZE

    def _show_size(self) -> None:
        """Spell out the size, because "32 GB" has two different meanings.

        A card sold as 32 GB holds 29.8 GiB, so an image built as 32 GiB is
        over two gigabytes too big for it.
        """
        text = self.quick_card_size.get_text()
        try:
            size = parse_size(text)
        except ValueError as error:
            self.quick_size_info.set_subtitle(str(error))
            return
        note = describe_size(size)
        card = size / 1000 ** 3
        if self.quick_target.get_selected() == 1:
            note += f" - needs a card of at least {card:.0f} GB"
        #  A bare G is binary, and that is the reading people do not expect: a
        #  card sold as 125 GB is 9 GB smaller than the 125 GiB "125G" asks
        #  for, and the image simply will not fit it.
        bare = text.strip().upper().rstrip()
        if bare and bare[-1] in "KMGT":
            decimal = f"{bare[:-1]}{bare[-1]}B"
            note += (f" - \u201c{text.strip()}\u201d is binary; write "
                     f"\u201c{decimal}\u201d for a card sold as that size")
        self.quick_size_info.set_subtitle(GLib.markup_escape_text(note))

    def _machine(self) -> machines.Machine:
        return machines.MACHINES[self.quick_machine.get_selected()]

    def _display(self) -> machines.Display:
        return list(machines.Display)[self.quick_display.get_selected()]

    def _prefer_rtg_screen(self) -> bool:
        """Whether Workbench is wanted on the RTG screen, where there is a choice."""
        return self.quick_workbench_screen.get_selected() == 0

    def _workbench_on_rtg(self) -> bool:
        return machines.workbench_on_rtg(self._display(),
                                         self._prefer_rtg_screen())

    def _on_display_changed(self) -> None:
        self._sync_visibility()
        self._on_layout_changed()

    def _on_machine_changed(self) -> None:
        if not self._ready:
            return
        machine = self._machine()
        self.quick_machine_hint.set_subtitle(
            f"{machine.board_label} - {machine.chipset.value} chipset "
            f"({machine.chipset.native_colours})")
        #  Which content categories are worth copying follows the machine.
        self._refresh_categories()
        #  Keep the Source page's board in step with the model.
        for index, variant in enumerate(emu68.VARIANTS):
            if variant.key == machine.board:
                self.variant_row.set_selected(index)
        self.quick_trapdoor.set_visible(machine.trapdoor_ram)
        self._relayout_partitions()
        self._quick_preview()

    def _detect_material(self) -> None:
        """Look for a Kickstart and Workbench disks, off the UI thread."""
        self.quick_found_rom.set_subtitle("Looking…")
        self.quick_found_adf.set_subtitle("Looking…")

        def work() -> None:
            try:
                found = presets.detect()
            except Exception as error:  # noqa: BLE001
                GLib.idle_add(self.quick_found_rom.set_subtitle, f"Search failed: {error}")
                return
            GLib.idle_add(self._material_found, found)

        threading.Thread(target=work, daemon=True).start()

    def _material_found(self, found: presets.Detected) -> bool:
        self.detected = found
        if found.kickstart:
            self.quick_found_rom.set_subtitle(
                f"{found.kickstart.name} - {found.kickstart.path.name}")
        else:
            self.quick_found_rom.set_subtitle(
                "None found. Add one on the Amiga page; Emu68 will not start "
                "without a Kickstart.")
        if found.adf_folder:
            state = "complete set" if found.adf_complete else "incomplete"
            self.quick_found_adf.set_subtitle(
                f"AmigaOS {found.adf_version} ({state}): {found.adf_summary}")
        else:
            self.quick_found_adf.set_subtitle(
                "None found. Put your Workbench ADFs in samples/ or choose a "
                "folder on the Amiga page.")
        if found.pfs3_donor and not self.quick_donor.path:
            self.quick_donor.set_path(found.pfs3_donor)
            self.quick_donor.set_subtitle(
                f"Found automatically - {found.pfs3_source}. PFS3 is not in "
                f"Kickstart, so a copy is embedded in the RDB or the partition "
                f"will not mount.")
        elif not found.pfs3_donor and not self.quick_donor.path:
            self.quick_donor.set_subtitle(
                "None found. PFS3 partitions will not mount without one - "
                "choose an .hdf or card image that contains PFS3.")
        self._quick_preview()
        return False

    def _quick_config(self) -> builder.BuildConfig:
        detected = dataclasses.replace(
            getattr(self, "detected", presets.Detected()),
            pfs3_donor=self.quick_donor.path)
        base = self.gather()
        try:
            system = parse_size(self.quick_system.get_text())
        except ValueError:
            system = presets.DEFAULT_SYSTEM_SIZE
        size = base.image_size
        if base.target_is_device:
            device = next((d for d in self.device_list if d.path == base.target), None)
            if device is not None:
                size = device.size
        hdmi_choice = bootcfg.HDMI_MODES[self.hdmi_row.get_selected()]
        return self._keep_other_pages(presets.machine_setup(
            self._machine(), self._display(), base.target,
            base.target_is_device, size, detected,
            pimiga_folder=self.quick_pimiga.path,
            hdmi=(hdmi_choice[1], hdmi_choice[2]),
            system_size=system, boot_size=self._boot_size(),
            trapdoor_to_chip=self.quick_trapdoor.get_active(),
            system_source=self._system_source(),
            hdf_source=self.quick_hdf.path,
            work_partition=self.quick_work.get_active(),
            package_donor=self._package_donor(),
            package_keys=self._chosen_packages(),
            prefer_rtg_screen=self._prefer_rtg_screen()), base)

    def _keep_other_pages(self, config: builder.BuildConfig,
                          base: builder.BuildConfig) -> builder.BuildConfig:
        """Put back the settings the quick setup does not decide."""
        options = dataclasses.replace(
            config.boot_options,
            extra_cmdline=merge_cmdline(config.boot_options.extra_cmdline,
                                        base.boot_options.extra_cmdline),
            **{name: getattr(base.boot_options, name)
               for name in KEPT_BOOT_OPTIONS})
        return dataclasses.replace(
            config, boot_options=options,
            **{name: getattr(base, name) for name in KEPT_ACROSS_QUICK_SETUP})

    def _on_layout_changed(self) -> None:
        """Something that shapes the partition layout has changed.

        Every choice that feeds the layout comes through here, because the rows
        are what a build actually reads: a size or a source that changed without
        redrawing them would build something other than what the page shows.
        """
        self._update_pimiga_info()
        self._relayout_partitions()
        self._quick_preview()

    def _on_source_changed(self) -> None:
        """A source changed, so the layout follows - and so does what is shown.

        Choosing "install Workbench from my floppy images" has to reveal the
        folder chooser, and only _sync_visibility ever sets that.  Sharing
        _on_layout_changed meant the choice was recorded, the partitions were
        redrawn, and the row that says where the disks are stayed hidden: the
        card could be told to install from floppies with no way to point at
        any.
        """
        self._on_layout_changed()
        self._sync_visibility()

    def _relayout_partitions(self) -> None:
        """Replace the partition rows with the layout the choices imply.

        Rows the user has edited by hand are left alone: they are only replaced
        while they still match what was last derived for them.
        """
        if not self._ready or getattr(self, "_relaying_out", False):
            return
        try:
            config = self._quick_config()
        except Exception:  # noqa: BLE001 - no target yet; nothing to lay out
            return
        current = [row.spec() for row in self.partition_rows]
        if current == config.amiga_partitions:
            return
        derived = getattr(self, "_derived_partitions", None)
        if derived is not None and current != derived:
            #  Hand-edited; redrawing would throw the user's work away.
            return
        self._relaying_out = True
        try:
            for row in list(self.partition_rows):
                self.partition_group.remove(row)
            self.partition_rows.clear()
            for spec in config.amiga_partitions:
                self._add_partition(spec)
            if not self.partition_rows:
                self._add_partition()
        finally:
            self._relaying_out = False
        #  Record what the rows now say, not what was asked for: a size shown
        #  as "10.55 GiB" does not read back as the exact byte count it came
        #  from, so comparing the two would call every layout hand-edited.
        self._derived_partitions = [row.spec() for row in self.partition_rows]

    def _update_pimiga_info(self) -> None:
        """Describe the chosen PiMiga folder, whatever else is still missing."""
        folder = self.quick_pimiga.path
        if not folder:
            self.quick_pimiga_info.set_subtitle("No folder selected")
            return
        disks = presets.pimiga_disks(folder)
        if disks is None:
            self.quick_pimiga_info.set_subtitle(
                "No PiMiga drives found there - expected System and Games "
                "folders, or a 'disks' folder containing them")
            return
        drives = [name for name in ("System", "Games", "Demos", "Work")
                  if (disks / name).is_dir()]
        text = f"Found {', '.join(drives)} in {disks}"
        left_out = presets.excluded_for(self._machine())
        if left_out:
            text += f"; leaving out {', '.join(left_out)}"
        self.quick_pimiga_info.set_subtitle(text)

    def _quick_preview(self) -> None:
        if not self._ready:
            return
        self._update_pimiga_info()
        #  When a source dictates the partition scheme this switch does nothing,
        #  so do not offer it: leaving it visible implied it was being obeyed.
        from_source = bool(self.quick_hdf.path) or (
            self.quick_pimiga.path
            and presets.pimiga_disks(self.quick_pimiga.path) is not None)
        self.quick_work.set_visible(not from_source)
        self.quick_system.set_visible(not self.quick_hdf.path)
        detected = dataclasses.replace(
            getattr(self, "detected", presets.Detected()),
            pfs3_donor=self.quick_donor.path)
        self._describe_plan(detected)

    def _describe_plan(self, detected=None) -> None:
        """Describe what will actually be written, not what was asked for.

        The plan used to come from the quick settings alone, so a partition
        edited on the Storage page changed the card and not a word of the
        description - which is the wrong way round, because this is the thing
        the user reads before pressing Write.  The real configuration is used
        when there is one, and the quick settings only stand in before a
        target has been chosen.
        """
        if detected is None:
            detected = dataclasses.replace(
                getattr(self, "detected", presets.Detected()),
                pfs3_donor=self.quick_donor.path)
        try:
            config = self.gather()
        except Exception:                        # noqa: BLE001 - no target yet
            try:
                config = self._quick_config()
            except Exception as error:           # noqa: BLE001
                self.quick_plan.set_text(str(error))
                return
        self.quick_plan.set_text(presets.describe_machine_setup(
            config, self._machine(), self._display(), detected))

    def _show_readiness(self, missing: list[str]) -> None:
        """Say what is still wanted, and only offer Apply when nothing is.

        Both Apply rows are kept in step: the one on the quick start's plan
        and the one at the end of the workflow are the same decision reached
        two ways.
        """
        if missing:
            first = missing[0]
            note = ("Still needed: " + first if len(missing) == 1
                    else f"Still needed: {first}, and {len(missing) - 1} more")
        else:
            note = "Accepts the setup above and enables Write"
        if getattr(self, "apply_note", None) is not None:
            self.apply_note.set_text(note)
            self.apply_button.set_sensitive(not missing)

    def _missing_choices(self) -> list[str]:
        """What still has to be decided before writing makes sense.

        validate() covers what would make the build fail outright; this is the
        rest - the things without which a card would be written and then not
        boot.  A Kickstart it has no ROM for, an install from floppies with no
        floppies.
        """
        try:
            config = self.gather()
        except Exception:                        # noqa: BLE001
            return ["a target to write to"]
        missing = [problem.rstrip(".") for problem in config.validate()]

        if config.mode is builder.BuildMode.IMAGE:
            #  A prepared system brings its own everything; the image and a
            #  card is the whole of it.
            return missing

        if not config.kickstart_path:
            missing.append("a Kickstart ROM")
        if config.install_emu68 and not config.emu68_archive \
                and not config.emu68_prepared_dir and not self.releases:
            missing.append("an Emu68 release - still looking, or choose a "
                           "local archive on the Source page")
        if config.install_amigaos:
            if not config.adf_folder:
                missing.append("a folder of Workbench floppy images")
            else:
                disks = getattr(self, "_adf_disks", None) or []
                if not disks:
                    missing.append("Workbench disks in that folder")
                else:
                    chosen = amigaos.choose_set(disks, config.adf_version)
                    gaps = amigaos.missing_roles(chosen)
                    if gaps:
                        missing.append("the "
                                       + ", ".join(r.label for r in gaps)
                                       + " disk")
        return missing

    def _on_apply_quick(self, _button) -> None:
        #  In the full workflow the pages *are* the configuration, so applying
        #  accepts what is there.  Regenerating it from the quick settings
        #  would undo the very customising that was asked for.
        if getattr(self, "_customising", False):
            try:
                self._applied_config = repr(self.gather())
            except Exception as error:           # noqa: BLE001
                self._toast(str(error))
                return
            self._update_summary()
            self._remember_session()
            self._toast("Setup accepted - Write is ready")
            return
        try:
            config = self._quick_config()
        except Exception as error:  # noqa: BLE001
            self._toast(str(error))
            return
        #  The layout is redrawn from the quick settings as they change, but
        #  only while nobody has touched it: once the partitions have been
        #  edited by hand, that stops.  Applying used to ignore the same rule
        #  and throw the edits away, so a carefully arranged set of drives
        #  reverted the moment the button was pressed.
        kept = self._hand_edited_partitions()
        if kept is not None:
            config = dataclasses.replace(config, amiga_partitions=kept)
        self.apply(config, keep_partitions=kept is not None)
        try:
            self._applied_config = repr(self.gather())
        except Exception:                        # noqa: BLE001
            self._applied_config = None
        self._update_summary()
        if kept is not None:
            self._toast("Quick setup applied; your own partitions were kept")
        else:
            self._toast("Quick setup applied and remembered for next time")
        #  Remember it now, not only on a clean exit: this is the point at
        #  which the setup is worth keeping.
        self._remember_session()

    def _quick_layout(self):
        """The layout the quick settings describe, for comparing against.

        An empty list when they cannot be read yet, which no real layout
        matches, so a loaded one is left alone rather than redrawn from
        settings that were not ready to say anything.
        """
        try:
            return list(self._quick_config().amiga_partitions)
        except Exception:                        # noqa: BLE001 - not ready
            return []

    def _hand_edited_partitions(self):
        """The partitions if they have been edited, else None.

        Compared against what was last derived from the quick settings, which
        is what the automatic relayout records for exactly this purpose.
        """
        derived = getattr(self, "_derived_partitions", None)
        if derived is None:
            return None
        current = [row.spec() for row in self.partition_rows]
        return current if current != derived else None

    def _page_source(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        self.page_source = page

        group = Adw.PreferencesGroup(title="What do you want to do?")
        self.mode_row = Adw.ComboRow(title="Task",
                                     model=combo([m[0] for m in MODES]))
        self.mode_row.connect("notify::selected", lambda *_a: self._sync_visibility())
        group.add(self.mode_row)
        self.mode_hint = Adw.ActionRow(title="", subtitle="")
        self.mode_hint.set_sensitive(False)
        group.add(self.mode_hint)
        page.add(group)

        self.image_group = Adw.PreferencesGroup(
            title="Pre-built image",
            description="A finished system such as CaffeineOS, an Emu68 "
                        "Hatcher image, or any .img backup of a card. "
                        "Download it from its author and point at the file; a "
                        "system this tool recognises is named, along with what "
                        "it expects of the machine. Compressed images (.xz, "
                        ".gz, .zip, .7z) are streamed straight to the card, so "
                        "no scratch space is needed.")
        self.image_row = FileRow("Image file", filters=IMAGE_FILTERS,
                                 on_change=lambda _p: self._on_image_chosen())
        self.image_group.add(self.image_row)
        self.image_info = Adw.ActionRow(title="Image details", subtitle="No image selected")
        self.image_info.set_sensitive(False)
        self.image_group.add(self.image_info)
        self.patch_display_row = Adw.SwitchRow(
            title="Adapt the display after writing",
            subtitle="A finished system keeps its own drivers, which are "
                     "right for it, but not its saved screen mode. Where "
                     "there is no RTG display, clear it so Workbench opens on "
                     "the Amiga's own screen instead of one that is not there.")
        self.patch_display_row.connect("notify::active",
                                       lambda *_a: self._update_summary())
        self.image_group.add(self.patch_display_row)
        page.add(self.image_group)

        page.add(self.group_primary)

        self.hdf_group = Adw.PreferencesGroup(
            title="Amiga hard disk image",
            description="An .hdf holding a Rigid Disk Block, as produced by "
                        "WinUAE, FS-UAE or HstWB Installer. This is the Amiga "
                        "drive only, so the card's partition table and boot "
                        "partition are created around it.")
        self.hdf_row = FileRow("Hard disk image (.hdf)", filters=HDF_FILTERS,
                               on_change=lambda _p: self._on_hdf_chosen())
        self.hdf_group.add(self.hdf_row)
        self.hdf_info = Adw.ActionRow(title="Image details", subtitle="No image selected")
        self.hdf_info.set_sensitive(False)
        self.hdf_group.add(self.hdf_info)
        self.repair_row = Adw.SwitchRow(
            title="Repair the drive for PiStorm compatibility",
            subtitle="Corrects RDB settings that cause corruption or stop the "
                     "drive mounting. Only metadata is changed, never your files.")
        self.repair_row.set_active(True)
        self.hdf_group.add(self.repair_row)
        self.hdf_check = Adw.ActionRow(title="Compatibility", subtitle="Not checked yet")
        self.hdf_check.set_sensitive(False)
        self.hdf_group.add(self.hdf_check)
        page.add(self.hdf_group)

        group = Adw.PreferencesGroup(
            title="Emu68",
            description="The 68k emulator that boots on the Raspberry Pi.")
        self.install_emu_row = Adw.SwitchRow(
            title="Install Emu68 on the boot partition",
            subtitle="Turn off to keep the Emu68 files already on the card")
        self.install_emu_row.set_active(True)
        self.install_emu_row.connect("notify::active", lambda *_a: self._sync_visibility())
        group.add(self.install_emu_row)

        self.variant_row = Adw.ComboRow(
            title="PiStorm board",
            model=combo([v.label for v in emu68.VARIANTS]))
        self.variant_row.connect("notify::selected", lambda *_a: self._on_variant_changed())
        group.add(self.variant_row)
        self.variant_hint = Adw.ActionRow(title="", subtitle=emu68.VARIANTS[0].description)
        self.variant_hint.set_sensitive(False)
        group.add(self.variant_hint)

        self.release_row = Adw.ComboRow(title="Emu68 version",
                                        model=combo(["Loading releases…"]))
        group.add(self.release_row)
        self.local_zip_row = FileRow(
            "Use a local Emu68 zip instead",
            "Leave empty to download the version chosen above",
            filters=ZIP_FILTERS)
        group.add(self.local_zip_row)
        page.add(group)
        return page

    def _page_amiga(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        self.page_amiga = page
        #  Which Amiga this is, and how it is being looked at, decides most of
        #  what follows on this page.
        page.add(self.group_hardware)

        group = Adw.PreferencesGroup(
            title="Kickstart ROM",
            description="Emu68 maps a Kickstart from the boot partition. An A1200 "
                        "(AGA) ROM is expected. Cloanto-encrypted ROMs are decrypted "
                        "automatically when rom.key sits beside them.")
        #  Kept, because the quick start borrows rom_row and has to be able
        #  to give it back.
        self.group_kickstart = group
        self.rom_row = FileRow("Kickstart ROM file", filters=ROM_FILTERS,
                               on_change=lambda _p: self._on_rom_chosen())
        group.add(self.rom_row)
        self.rom_key_row = FileRow("Cloanto rom.key (optional)",
                                   "Only needed for encrypted Amiga Forever ROMs")
        group.add(self.rom_key_row)
        self.rom_info = Adw.ActionRow(title="ROM details", subtitle="No ROM selected")
        self.rom_info.set_sensitive(False)
        group.add(self.rom_info)
        page.add(group)

        self.os_group = Adw.PreferencesGroup(
            title="Workbench floppy images",
            description="Used when the operating system is set to \u201cinstall "
                        "from my floppy images\u201d on the Quick setup page. "
                        "Disks are recognised by the volume name inside them, "
                        "not by file name.")
        self.adf_row = FileRow("Folder containing the ADF disks", folder=True,
                               on_change=lambda _p: self._scan_adfs())
        self.os_group.add(self.adf_row)
        self.os_version_row = Adw.ComboRow(title="AmigaOS release",
                                           model=combo(["Choose a folder first"]))
        self.os_version_row.connect("notify::selected", lambda *_a: self._show_disk_set())
        self.os_group.add(self.os_version_row)
        self.volume_row = Adw.EntryRow(title="Volume name")
        self.volume_row.set_text("Workbench")
        self.os_group.add(self.volume_row)
        self.os_disks = Adw.ActionRow(title="Disks found", subtitle="No folder selected")
        self.os_disks.set_sensitive(False)
        self.os_group.add(self.os_disks)
        page.add(self.os_group)

        self.packages_group = Adw.PreferencesGroup(
            title="Software to add",
            description="A Workbench built from the original disks is exactly "
                        "what shipped in 1994: no archiver, no installer, no "
                        "WHDLoad. Freely distributable pieces are fetched from "
                        "Aminet and cached; anything that is not - IBrowse and "
                        "the like - is only ever copied out of a system you "
                        "already have, such as a PiMiga installation.")
        self.package_donor = FileRow(
            "Take it from", "A Workbench System drive, or a PiMiga folder",
            folder=True, on_change=lambda _p: self._refresh_packages())
        self.packages_group.add(self.package_donor)
        suggest = Adw.ActionRow(
            title="Suggested load",
            subtitle="Tick what suits this machine, chipset and display")
        button = Gtk.Button(label="Apply")
        button.set_valign(Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.connect("clicked", lambda *_a: self._apply_suggested_packages())
        suggest.add_suffix(button)
        suggest.set_activatable_widget(button)
        self.packages_group.add(suggest)
        page.add(self.packages_group)

        #  One group per category, so a long list reads as a few short ones.
        self.package_rows: dict[str, Adw.SwitchRow] = {}
        self.package_groups: list[Adw.PreferencesGroup] = [self.packages_group]
        for category in packages.Category:
            members = packages.in_category(category)
            if not members:
                continue
            group = Adw.PreferencesGroup(title=category.value)
            for package in members:
                row = Adw.SwitchRow(title=package.label,
                                    subtitle=package.description)
                row.set_active(package.default)
                row.connect("notify::active",
                            lambda *_a, key=package.key:
                            self._on_package_toggled(key))
                self.package_rows[package.key] = row
                group.add(row)
            self.package_groups.append(group)
            page.add(group)
        #  The defaults are set row by row above, which never goes through the
        #  toggle, so what they need has to be ticked once they all exist.
        self._tick_what_is_needed()
        return page

    def _page_storage(self) -> Adw.PreferencesPage:
        """How the card is divided up.

        Kept apart from the Amiga page deliberately: how big the drives are and
        what file system they carry is a different question from what gets
        written into them, and mixing the two made a long page where neither
        was easy to find.
        """
        page = Adw.PreferencesPage()

        page.add(self.group_sizes)

        self.partition_group = Adw.PreferencesGroup(
            title="Amiga partitions",
            description="Written as a Rigid Disk Block inside the 0x76 partition. "
                        "Each one can be filled from the Amiga page, or left "
                        "empty to format from HDToolBox on the Amiga.")
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Add a partition")
        add.add_css_class("flat")
        add.connect("clicked", lambda _b: self._add_partition())
        self.partition_group.set_header_suffix(add)
        page.add(self.partition_group)
        self.partition_rows: list[PartitionRow] = []
        self._add_partition(builder.AmigaPartitionSpec("DH0", None, "PFS3", True, 0))

        self.expand_group = Adw.PreferencesGroup(
            title="Unused space",
            description="A pre-built image is usually smaller than the card. The "
                        "leftover space can become a new Amiga partition - existing "
                        "partitions are never resized, so nothing on the card is at risk.")
        self.expand_row = Adw.SwitchRow(
            title="Add a partition in the unused space",
            subtitle="Format it on the Amiga afterwards")
        self.expand_row.connect("notify::active", lambda *_a: self._sync_visibility())
        self.expand_group.add(self.expand_row)
        add_extra = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Add another partition")
        add_extra.add_css_class("flat")
        add_extra.connect("clicked", lambda _b: self._add_extra_partition())
        self.expand_group.set_header_suffix(add_extra)
        page.add(self.expand_group)
        self.extra_rows: list[PartitionRow] = []
        self._add_extra_partition(
            builder.AmigaPartitionSpec("DH1", None, "PFS3", False, -128))
        return page

    def _page_options(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(
            title="Display",
            description="The Raspberry Pi fixes its HDMI output at boot. Workbench "
                        "and RTG screens are scaled to it, so match your monitor.")
        self.hdmi_row = Adw.ComboRow(
            title="HDMI output", model=combo([m[0] for m in bootcfg.HDMI_MODES]))
        group.add(self.hdmi_row)
        page.add(group)

        group = Adw.PreferencesGroup(
            title="Raspberry Pi",
            description="Leave these untouched to keep whatever the Emu68 release ships.")
        self.overclock_row = Adw.ComboRow(
            title="CPU speed",
            model=combo(["As shipped with Emu68", "Overclock to 1.8 GHz", "No overclock"]))
        group.add(self.overclock_row)
        self.antenna_row = Adw.ComboRow(
            title="CM4 WiFi antenna",
            model=combo(["As shipped with Emu68", "External antenna", "Internal antenna"]))
        group.add(self.antenna_row)
        page.add(group)

        group = Adw.PreferencesGroup(
            title="Emu68 options",
            description="Written to cmdline.txt. See the Emu68 documentation for the "
                        "full list.")
        self.vc4_row = Adw.SpinRow.new_with_range(0, 512, 16)
        self.vc4_row.set_title("Picasso96 video memory (MB)")
        self.vc4_row.set_subtitle("0 leaves the Emu68 default of 16 MB")
        self.vc4_row.set_value(0)
        group.add(self.vc4_row)
        self.vbr_row = Adw.SwitchRow(
            title="Move the vector base register to fast RAM",
            subtitle="Faster, but it moves the interrupt vectors away from "
                     "address 0, where games and demos that take over the "
                     "machine expect to install their own. That includes "
                     "WHDLoad titles run from the hard drive - it is how the "
                     "software was written, not where it is loaded from")
        group.add(self.vbr_row)
        self.slowdown_row = Adw.SwitchRow(
            title="Chip RAM slowdown",
            subtitle="For OCS and ECS software that busy-waits on the "
                     "chipset, which a PiStorm otherwise runs straight past. "
                     "Set for you on an A500, A500+, A600, A1000 or A2000")
        group.add(self.slowdown_row)
        self.dbf_row = Adw.SwitchRow(
            title="DBF loop slowdown",
            subtitle="For OCS and ECS era software that times itself with a "
                     "delay loop and runs far too fast on a PiStorm")
        group.add(self.dbf_row)
        self.blitwait_row = Adw.SwitchRow(
            title="Wait for the blitter",
            subtitle="For OCS and ECS software that starts a blit and reads "
                     "the result without waiting, which only worked because "
                     "the real chipset was slower")
        group.add(self.blitwait_row)
        self.swapdf_row = Adw.SwitchRow(title="Swap DF0: with DF1:")
        group.add(self.swapdf_row)
        self.unit0_row = Adw.SwitchRow(
            title="Allow the Amiga to write to the whole SD card",
            subtitle="Exposes the partition table and boot partition read/write")
        group.add(self.unit0_row)
        self.extra_row = Adw.EntryRow(title="Additional cmdline.txt options")
        group.add(self.extra_row)
        page.add(group)

        group = Adw.PreferencesGroup(
            title="WiFi",
            description="Stored on the boot partition in clear text for the Amiga-side "
                        "PiStorm WiFi tools. Leave empty to skip.")
        self.ssid_row = Adw.EntryRow(title="Network name (SSID)")
        group.add(self.ssid_row)
        self.psk_row = Adw.PasswordEntryRow(title="Password")
        group.add(self.psk_row)
        self.country_row = Adw.EntryRow(title="Country code")
        self.country_row.set_text("GB")
        group.add(self.country_row)
        page.add(group)
        return page

    def _page_target(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        self.page_target = page

        group = Adw.PreferencesGroup(title="Where should the result go?")
        self.target_row = Adw.ComboRow(
            title="Write to",
            model=combo(["SD card", "SD card image file",
                         "Amiga hard disk image (.hdf)"]))
        self.target_row.connect("notify::selected", lambda *_a: self._sync_visibility())
        group.add(self.target_row)
        page.add(group)

        self.device_group = Adw.PreferencesGroup(
            title="SD card",
            description="Only removable drives are listed. Everything on the chosen "
                        "card will be destroyed.")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text="Rescan for cards")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda _b: self._refresh_devices())
        self.device_group.set_header_suffix(refresh)
        self.device_row = Adw.ComboRow(title="Card", model=combo(["No cards found"]))
        self.device_row.connect("notify::selected",
                                lambda *_a: (self._follow_the_card(),
                                             self._update_summary()))
        self.device_group.add(self.device_row)
        page.add(self.device_group)

        self.file_group = Adw.PreferencesGroup(
            title="Image file",
            description="A sparse .img file you can write to a card later, or use "
                        "with an emulator.")
        self.file_row = SaveRow("Save image as", filters=IMAGE_FILTERS,
                                on_change=lambda _p: self._update_summary())
        self.file_group.add(self.file_row)
        self.file_size_row = Adw.EntryRow(
            title="Image size - 32GB as cards are sold, 32GiB binary")
        self.file_size_row.set_text("32GB")
        self.file_size_row.connect("changed", lambda _r: self._update_summary())
        self.file_group.add(self.file_size_row)
        page.add(self.file_group)

        self.boot_group = Adw.PreferencesGroup(
            title="Boot partition",
            description="Holds Emu68, the Raspberry Pi firmware and your Kickstart.")
        self.boot_size_row = Adw.EntryRow(title="Size")
        self.boot_size_row.set_text("256M")
        self.boot_size_row.connect("changed",
                                   lambda _r: self._on_layout_changed())
        self.boot_group.add(self.boot_size_row)
        page.add(self.boot_group)
        return page

    # --------------------------------------------------------- progress UI

    def _build_progress(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Writing", subtitle=""))
        self.progress_title = header.get_title_widget()
        view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.step_label = Gtk.Label(label="Starting…", xalign=0.0)
        self.step_label.add_css_class("title-4")
        box.append(self.step_label)
        self.progress_bar = Gtk.ProgressBar(show_text=True)
        box.append(self.progress_bar)

        self.log_view = Gtk.TextView(editable=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_buffer = self.log_view.get_buffer()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.log_view)
        scroller.add_css_class("card")
        box.append(scroller)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                          halign=Gtk.Align.END)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.add_css_class("destructive-action")
        self.cancel_button.connect("clicked", self._on_cancel)
        buttons.append(self.cancel_button)
        self.back_button = Gtk.Button(label="Back")
        self.back_button.set_visible(False)
        self.back_button.connect("clicked",
                                 lambda _b: self.outer.set_visible_child_name("setup"))
        buttons.append(self.back_button)
        self.save_log_button = Gtk.Button(label="Save log…")
        self.save_log_button.set_visible(False)
        self.save_log_button.connect("clicked", self._on_save_log)
        buttons.append(self.save_log_button)
        box.append(buttons)

        view.set_content(box)
        return view

    # ------------------------------------------------------------- helpers

    def _mode(self) -> builder.BuildMode:
        return MODES[self.mode_row.get_selected()][1]

    def _writing_to_device(self) -> bool:
        return self.target_row.get_selected() == 0

    def _selected_device(self):
        """The card chosen to be written to, or None if none is."""
        if not self._writing_to_device():
            return None
        index = self.device_row.get_selected() - 1   # row 0 is the placeholder
        if not self.device_list or index < 0 or index >= len(self.device_list):
            return None
        return self.device_list[index]

    def _making_hdf(self) -> bool:
        """True when the output is a bare Amiga drive rather than a card."""
        return self.target_row.get_selected() == 2

    def _sync_visibility(self) -> None:
        if not self._ready:
            return
        mode = self._mode()
        making_hdf = self._making_hdf()
        primary = self._primary()
        for row in (self.quick_pimiga, self.quick_pimiga_info):
            row.set_visible(primary == "pimiga")
        for row in (self.quick_hdf, self.quick_hdf_info):
            row.set_visible(primary == "image")
        for row in (self.quick_system_source, self.quick_os_hint):
            row.set_visible(primary == "default")
        self.quick_workbench_screen.set_visible(
            self._display().has_choice_of_screen)
        self.mode_hint.set_subtitle(MODES[self.mode_row.get_selected()][2])
        #  On the quick start the screen decides what is on show, not the
        #  task mode; letting both set it made the image chooser flicker in
        #  and out as the mode was adjusted underneath.
        if getattr(self, "_customising", True):
            self.image_group.set_visible(mode is builder.BuildMode.IMAGE)
        self.hdf_group.set_visible(mode is builder.BuildMode.HDF)
        self.partition_group.set_visible(mode is builder.BuildMode.FRESH)
        self.os_group.set_visible(mode is builder.BuildMode.FRESH)
        #  Anything this build lays out can have software added to it, not
        #  only a Workbench installed from floppies: an imported drive gets
        #  the same package overlays, and hiding the list meant a card built
        #  around somebody's drive could not be given WHDLoad or iGame.
        show_packages = mode is builder.BuildMode.FRESH
        for group in self.package_groups:
            group.set_visible(show_packages)
        #  The floppies are offered alongside an imported drive too: a drive
        #  can boot and still bring no Workbench of its own - ClassicWB's
        #  asks for the disks on its first boot - and there was no way to
        #  say where they are.
        installing = (self._system_source() == "adf"
                      or (self.quick_hdf.path and self._imported_needs_floppies()))
        for row in (self.adf_row, self.os_version_row, self.volume_row, self.os_disks):
            row.set_visible(installing)
        self.expand_group.set_visible(mode is not builder.BuildMode.FRESH)
        for row in self.extra_rows:
            row.set_visible(self.expand_row.get_active())
        install = self.install_emu_row.get_active() and not making_hdf
        self.install_emu_row.set_visible(not making_hdf)
        for row in (self.variant_row, self.variant_hint, self.release_row,
                    self.local_zip_row):
            row.set_visible(install)
        self.device_group.set_visible(self._writing_to_device())
        self.file_group.set_visible(not self._writing_to_device())
        self.file_group.set_title("Amiga hard disk image" if making_hdf
                                  else "Image file")
        self.file_group.set_description(
            "A bare Amiga drive with a Rigid Disk Block and no boot partition - "
            "usable here, and in WinUAE or FS-UAE." if making_hdf
            else "A sparse .img file you can write to a card later, or use with "
                 "an emulator.")
        partitions_ours = mode in (builder.BuildMode.FRESH, builder.BuildMode.HDF)
        self.file_size_row.set_visible(not self._writing_to_device() and partitions_ours)
        self.file_size_row.set_title(
            "Drive size" if making_hdf
            else "Image size - 32GB as cards are sold, 32GiB binary")
        self.boot_group.set_visible(partitions_ours and not making_hdf)
        self._update_summary()

    def _on_variant_changed(self) -> None:
        if not self._ready:
            return
        variant = emu68.VARIANTS[self.variant_row.get_selected()]
        self.variant_hint.set_subtitle(variant.description)
        self._populate_releases()

    def _load_releases_async(self) -> None:
        def work() -> None:
            try:
                found = emu68.fetch_releases()
            except Exception as error:  # noqa: BLE001 - offline is not fatal
                GLib.idle_add(self._releases_failed, str(error))
                return
            GLib.idle_add(self._releases_loaded, found)

        threading.Thread(target=work, daemon=True).start()

    def _releases_loaded(self, found: list[emu68.Release]) -> bool:
        self.releases = found
        self._populate_releases()
        return False

    def _releases_failed(self, message: str) -> bool:
        self.release_row.set_model(combo(["Could not reach GitHub"]))
        self.release_row.set_subtitle(
            f"{message}. Choose a local Emu68 zip below instead.")
        return False

    def _populate_releases(self) -> None:
        if not self.releases:
            return
        variant = emu68.VARIANTS[self.variant_row.get_selected()].key
        self._release_choices = [r for r in self.releases
                                 if emu68.has_variant(r, variant)]
        labels = [f"{r.display()} - {r.published}" for r in self._release_choices]
        self.release_row.set_model(combo(labels or ["No build for this board"]))
        #  A setup that was loaded asked for a particular build, and the list
        #  it has to be found in arrives from GitHub after the setup does.
        #  Falling straight to the newest stable one quietly swapped a card
        #  built against a beta onto a different Emu68 altogether.
        wanted = getattr(self, "_wanted_release", "")
        if wanted:
            for index, release in enumerate(self._release_choices):
                if release.tag == wanted:
                    self.release_row.set_selected(index)
                    #  Honoured once: choosing another board afterwards should
                    #  offer that board's newest build, not this tag for ever.
                    self._wanted_release = ""
                    return
        for index, release in enumerate(self._release_choices):
            if not release.prerelease:
                self.release_row.set_selected(index)
                break

    def _add_partition(self, spec: builder.AmigaPartitionSpec | None = None) -> None:
        if len(self.partition_rows) >= 10:
            self._toast("An RDB here is limited to 10 partitions.")
            return
        if spec is None:
            index = len(self.partition_rows)
            spec = builder.AmigaPartitionSpec(f"DH{index}", None, "PFS3", index == 0,
                                              0 if index == 0 else -128)
        row = PartitionRow(spec, self._remove_partition,
                           self._update_summary, machine=self._machine)
        self.partition_rows.append(row)
        self.partition_group.add(row)
        self._update_summary()

    def _add_extra_partition(self,
                             spec: builder.AmigaPartitionSpec | None = None) -> None:
        """Add an editor row for a partition to create in an imported drive's
        unused space."""
        if len(self.extra_rows) >= 9:
            self._toast("An RDB here is limited to 10 partitions.")
            return
        if spec is None:
            index = len(self.extra_rows) + 1
            spec = builder.AmigaPartitionSpec(f"DH{index}", None, "PFS3", False, -128)
        row = PartitionRow(spec, self._remove_extra_partition,
                           self._update_summary, machine=self._machine)
        self.extra_rows.append(row)
        self.expand_group.add(row)
        row.set_visible(self.expand_row.get_active())
        self._update_summary()

    def _remove_extra_partition(self, row: PartitionRow) -> None:
        if len(self.extra_rows) == 1:
            self._toast("Keep at least one partition, or turn the switch off.")
            return
        self.extra_rows.remove(row)
        self.expand_group.remove(row)
        self._update_summary()

    def _remove_partition(self, row: PartitionRow) -> None:
        if len(self.partition_rows) == 1:
            self._toast("At least one Amiga partition is required.")
            return
        self.partition_rows.remove(row)
        self.partition_group.remove(row)
        self._update_summary()

    def _package_donor(self) -> str:
        """Where optional software is copied from."""
        return self.package_donor.path or self.quick_pimiga.path

    def _apply_suggested_packages(self) -> None:
        """Tick the set that suits the machine and screen that are chosen."""
        wanted = set(packages.suggested(
            self._machine(), self._display(),
            donor=self._package_donor() or None,
            networking=bool(self.wifi_ssid.get_text().strip())))
        for key, row in self.package_rows.items():
            if row.get_sensitive():
                row.set_active(key in wanted)
        self._tick_what_is_needed()
        self._refresh_packages()

    def _refresh_categories(self) -> None:
        """Re-default every partition's categories for the machine now chosen."""
        for row in list(self.partition_rows) + list(self.extra_rows):
            row.reload_categories()

    def _refresh_packages(self) -> None:
        """Offer only the software that can actually be obtained and used."""
        if not self._ready:
            return
        donor = self._package_donor()
        found = packages.available(donor) if donor else {}
        display = self._display()
        chipset = self._machine().chipset
        for key, row in self.package_rows.items():
            package = packages.CATALOGUE_BY_KEY[key]
            fits = package.suits(chipset, display)
            downloadable = package.download is not None
            usable = fits and (key in found or downloadable)
            row.set_sensitive(usable)
            note = package.description
            if not fits and package.rtg_only:
                note += "  -  only useful with an RTG display."
            elif not fits and package.native_only:
                note += "  -  only useful on the Amiga's own screen."
            elif not fits:
                note += "  -  not a fit for this chipset."
            elif key in found:
                note += "  -  from your donor system."
            elif downloadable:
                where = package.download.source or "Aminet"
                if package.download.manual:
                    #  Nothing here can fetch it; the build uses a copy the
                    #  user has put in the cache, so say so before the build
                    #  rather than in the log afterwards.
                    note += f"  -  supply the archive yourself, from {where}."
                else:
                    note += f"  -  will be fetched from {where}."
                if package.note:
                    note += f" {package.note}"
            else:
                note += ("  -  needs a donor system that has it."
                         if donor else
                         "  -  choose where to take it from first.")
            row.set_subtitle(GLib.markup_escape_text(note))
            if not usable:
                row.set_active(False)
        self._on_layout_changed()

    def _needed_by_active(self, key: str, ignoring: str = "") -> bool:
        """Whether anything still switched on requires this package."""
        for other, row in self.package_rows.items():
            if other in (key, ignoring) or not row.get_active():
                continue
            if key in packages.expand([other]):
                return True
        return False

    def _tick_what_is_needed(self) -> None:
        """Switch on whatever the ticked packages require.

        Ticking one switches on what it needs, but a package ticked by
        default - or by loading a setup, or by the suggested load - never
        passed through that, so iGame arrived ticked with MUI and its classes
        beside it switched off. They were installed anyway; the page simply
        did not say so, and turning iGame off and on again "fixed" it.
        """
        for key, row in list(self.package_rows.items()):
            if not row.get_active():
                continue
            for needed in packages.expand([key]):
                other = self.package_rows.get(needed)
                if other is not None and needed != key and not other.get_active():
                    other.set_active(True)

    def _on_package_toggled(self, key: str) -> None:
        """Keep the page honest about what a choice drags along with it.

        On: what it needs comes with it. Off: anything that needed *it* goes
        too - a browser without MUI is not a browser - and so does anything
        that was only ever there to satisfy something else and now satisfies
        nothing. A package worth having on its own stays: turning off one MUI
        program should not take MUI away from the rest.
        """
        if getattr(self, "_settling_packages", False):
            return
        row = self.package_rows.get(key)
        self._settling_packages = True
        try:
            if row is not None and row.get_active():
                for needed in packages.expand([key]):
                    other = self.package_rows.get(needed)
                    if other is not None and needed != key:
                        other.set_active(True)
            elif row is not None:
                #  Whatever required it cannot work without it.
                for other_key, other_row in list(self.package_rows.items()):
                    if other_key == key or not other_row.get_active():
                        continue
                    if key in packages.expand([other_key]):
                        other_row.set_active(False)
                #  Then let go of anything that was only propping this up.
                for gone in packages.expand([key]):
                    package = packages.CATALOGUE_BY_KEY.get(gone)
                    other = self.package_rows.get(gone)
                    if (gone != key and package is not None and other is not None
                            and package.support_only and other.get_active()
                            and not self._needed_by_active(gone)):
                        other.set_active(False)
        finally:
            self._settling_packages = False
        self._on_layout_changed()

    def _chosen_packages(self) -> list[str]:
        return [key for key, row in self.package_rows.items()
                if row.get_active() and row.get_sensitive()]

    def _refresh_devices(self) -> None:
        if not self._ready:
            return
        try:
            self.device_list = devices.list_devices(only_removable=True)
        except RuntimeError as error:
            self.device_list = []
            self._toast(str(error))
        #  Never preselect a device.  "Removable" includes any USB disk, so a
        #  default selection could quietly point a destructive write at the
        #  user's backup drive; make choosing the card a deliberate act.
        if self.device_list:
            labels = [SELECT_CARD] + [d.description for d in self.device_list]
        else:
            labels = ["No removable cards found - insert one and press refresh"]
        self.device_row.set_model(combo(labels))
        self.device_row.set_selected(0)
        if hasattr(self, "quick_device"):
            self.quick_device.set_model(combo(labels))
            self.quick_device.set_selected(0)
        self._update_summary()

    def _known_card_size(self) -> int:
        """The card size if one has been said, for a "is it big enough" check."""
        try:
            return parse_size(self.quick_card_size.get_text())
        except Exception:                        # noqa: BLE001 - not set yet
            return 0

    def _on_image_chosen(self) -> None:
        from ..core import imgsrc
        if not self.image_row.path:
            self.image_info.set_subtitle("No image selected")
            self._update_summary()
            return
        try:
            source = imgsrc.inspect(self.image_row.path)
            description = source.description
        except Exception as error:  # noqa: BLE001
            self.image_info.set_subtitle(f"Cannot read this file: {error}")
            self._update_summary()
            return
        #  Naming the system, and saying what it expects, is worth more than
        #  the file's dimensions: a card gets committed to one of these.
        found = distributions.identify(self.image_row.path)
        if found is not None:
            notes = distributions.describe(found, self._known_card_size())
            if found.rtg_only and not self._display().uses_rtg:
                notes.append("The display is set to the Amiga's own video "
                             "output, where this system shows nothing.")
            description = f"{found.label} - " + description + "\n" + "\n".join(
                "- " + line for line in notes)
        self.image_info.set_subtitle(description)
        self._update_summary()

    def _scan_adfs(self) -> None:
        """Identify the ADFs in the chosen folder, off the UI thread."""
        folder = self.adf_row.path
        if not folder:
            self.os_disks.set_subtitle("No folder selected")
            self._adf_disks = []
            self._update_summary()
            return
        self.os_disks.set_subtitle("Scanning…")

        def work() -> None:
            try:
                disks = amigaos.scan(folder)
            except Exception as error:  # noqa: BLE001
                GLib.idle_add(self.os_disks.set_subtitle, f"Cannot scan: {error}")
                return
            GLib.idle_add(self._adfs_scanned, disks)

        threading.Thread(target=work, daemon=True).start()

    def _adfs_scanned(self, disks) -> bool:
        self._adf_disks = disks
        versions = amigaos.available_versions(disks)
        self._adf_versions = versions
        if versions:
            self.os_version_row.set_model(combo([f"AmigaOS {v}" for v in versions]))
            self.os_version_row.set_selected(0)
        else:
            self.os_version_row.set_model(combo(["No Workbench disk found"]))
        self._show_disk_set()
        return False

    def _show_disk_set(self) -> None:
        disks = getattr(self, "_adf_disks", [])
        versions = getattr(self, "_adf_versions", [])
        if not disks:
            self.os_disks.set_subtitle("No Workbench disks found in that folder")
            self._update_summary()
            return
        index = self.os_version_row.get_selected()
        version = versions[index] if index < len(versions) else ""
        chosen = amigaos.choose_set(disks, version)
        found = ", ".join(m.role.label for m in
                          sorted(chosen.values(), key=lambda m: m.role.order))
        missing = amigaos.missing_roles(chosen)
        text = f"{found or 'none'}"
        if missing:
            text += "  -  MISSING: " + ", ".join(r.label for r in missing)
        else:
            text += f"  -  about {human_size(amigaos.estimate_size(chosen))} installed"
        self.os_disks.set_subtitle(text)
        self._update_summary()

    def _on_hdf_chosen(self) -> None:
        if not self.hdf_row.path:
            self.hdf_info.set_subtitle("No image selected")
            self.hdf_check.set_subtitle("Not checked yet")
            self._update_summary()
            return
        try:
            info = builder.inspect_hdf(self.hdf_row.path)
        except Exception as error:  # noqa: BLE001
            self.hdf_info.set_subtitle(f"Cannot read this file: {error}")
            self.hdf_check.set_subtitle("Not checked")
            self._update_summary()
            return

        self.hdf_info.set_subtitle(info.description)
        if info.table is None:
            if info.bare_dostype is not None:
                self.hdf_check.set_subtitle(
                    "No Rigid Disk Block; one will be created around this "
                    "file system so Emu68 can mount it.")
            else:
                self.hdf_check.set_subtitle(
                    "No RDB and no recognisable Amiga file system - this file "
                    "cannot be used as an Amiga drive.")
        else:
            capacity = info.source_length or info.size
            findings = hdfcheck.analyse(info.table, capacity)
            summary = hdfcheck.summarise(findings)
            worst = [f for f in findings if f.severity == hdfcheck.ERROR]
            if worst:
                summary += ".  " + worst[0].message
            self.hdf_check.set_subtitle(summary)
        self._update_summary()

    def _on_rom_chosen(self) -> None:
        if not self.rom_row.path:
            self.rom_info.set_subtitle("No ROM selected")
            return
        try:
            info = kickstart.identify(self.rom_row.path, self.rom_key_row.path or None)
        except Exception as error:  # noqa: BLE001
            self.rom_info.set_subtitle(f"Cannot read this file: {error}")
            return
        parts = [info.name, human_size(info.size)]
        if info.note:
            parts.append(info.note)
        if info.version and not info.aga:
            parts.append("WARNING: not an A1200/AGA ROM")
        if not info.usable:
            parts.append("this file cannot be used")
        self.rom_info.set_subtitle(" · ".join(parts))

    def _toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))

    def _update_summary(self) -> None:
        if not self._ready:
            return
        #  The plan reads from the same configuration, so a partition edited
        #  on the Storage page shows up in it.
        self._describe_plan()
        missing = self._missing_choices()
        self._show_readiness(missing)
        try:
            config = self.gather()
        except Exception as error:  # noqa: BLE001 - partial input while typing
            self.summary.set_text(str(error))
            self.write_button.set_sensitive(False)
            return
        if missing:
            self.summary.set_text("Still needed: " + missing[0])
            self.write_button.set_sensitive(False)
            return
        #  Comparing the configuration itself, rather than trying to notice
        #  every widget that could change it: anything that alters what would
        #  be written puts the setup back to needing another look.
        applied = repr(config) == getattr(self, "_applied_config", None)
        target = config.target
        if config.mode is builder.BuildMode.IMAGE:
            what = f"Write {Path(config.source_image).name}"
        elif config.mode is builder.BuildMode.HDF:
            what = f"Build a card around {Path(config.hdf_image).name}"
        if config.output_hdf:
            what = "Create an Amiga hard disk image"
        elif config.mode is builder.BuildMode.FRESH:
            what = "Partition and build"
        else:
            what = "Update the boot partition of"
        if applied:
            self.summary.set_text(f"{what} → {target}")
        else:
            self.summary.set_text(f"{what} → {target}"
                                  "   -   Apply this setup to enable Write")
        self.write_button.set_sensitive(applied)
        self._quick_preview()

    # ------------------------------------------------------- config gather

    def gather(self) -> builder.BuildConfig:
        mode = self._mode()
        if self._writing_to_device():
            index = self.device_row.get_selected() - 1   # row 0 is the placeholder
            if not self.device_list or index < 0 or index >= len(self.device_list):
                raise ValueError("No SD card selected.")
            target, is_device = self.device_list[index].path, True
        else:
            target, is_device = self.file_row.path, False

        hdmi = bootcfg.HDMI_MODES[self.hdmi_row.get_selected()]
        overclock = {0: None, 1: True, 2: False}[self.overclock_row.get_selected()]
        antenna = {0: None, 1: True, 2: False}[self.antenna_row.get_selected()]
        vc4 = int(self.vc4_row.get_value())

        options = bootcfg.BootOptions(
            hdmi_group=hdmi[1], hdmi_mode=hdmi[2],
            hdmi_automatic=hdmi[1] is None,
            overclock=overclock,
            cm4_external_antenna=antenna,
            vc4_mem=vc4 or None,
            vbr_move=self.vbr_row.get_active(),
            chip_slowdown=self.slowdown_row.get_active(),
            dbf_slowdown=self.dbf_row.get_active(),
            blitwait=self.blitwait_row.get_active(),
            swap_df0_with_df1=self.swapdf_row.get_active(),
            sd_unit0_rw=self.unit0_row.get_active(),
            extra_cmdline=self._extra_cmdline(),
        )

        release_tag = ""
        choices = getattr(self, "_release_choices", [])
        if choices:
            index = min(self.release_row.get_selected(), len(choices) - 1)
            release_tag = choices[index].tag

        card = self._selected_device()
        if card is not None and card.size:
            #  Writing to a card: its capacity is the size, whatever any box
            #  says. Typing it invited "125G" for a card sold as 125 GB, which
            #  is 125 GiB - 9 GB more than the card holds.
            image_size = card.size
        else:
            try:
                image_size = parse_size(self.file_size_row.get_text())
            except ValueError:
                image_size = 8 * GIB
        boot_size = self._boot_size()

        return builder.BuildConfig(
            mode=mode,
            target=target,
            target_is_device=is_device,
            image_size=image_size,
            variant=emu68.VARIANTS[self.variant_row.get_selected()].key,
            release_tag=release_tag,
            emu68_archive=self.local_zip_row.path,
            install_emu68=(self.install_emu_row.get_active()
                           and not self._making_hdf()),
            source_image=self.image_row.path,
            hdf_image=self.hdf_row.path,
            output_hdf=self._making_hdf(),
            repair_rdb=self.repair_row.get_active(),
            patch_display=self.patch_display_row.get_active(),
            boot_size=boot_size,
            amiga_partitions=[row.spec() for row in self.partition_rows],
            pfs3_binary=self.quick_donor.path,
            #  Only a card we are partitioning ourselves can have an OS
            #  installed onto it from floppies.
            install_amigaos=(mode is builder.BuildMode.FRESH
                             and not self.quick_hdf.path
                             and self._system_source() == "adf"
                             and bool(self.adf_row.path)),
            adf_folder=self.adf_row.path,
            adf_version=self._selected_adf_version(),
            amiga_volume_name=self.volume_row.get_text().strip() or "Workbench",
            #  The software chosen on the Amiga page.  These only used to be
            #  set by the quick setup, so ticking a package and pressing Write
            #  from the pages themselves quietly built a card without it.
            package_donor=self._package_donor(),
            package_keys=self._chosen_packages(),
            package_chipset=self._machine().chipset.value,
            package_display=self._display().value,
            #  The display choice lives on the Quick setup page but decides
            #  what happens to a copied system's graphics setup, so it has to
            #  reach every build - not only one started from that page.
            system_source=self._system_source(),
            rtg_display=self._display().uses_rtg,
            native_display=self._display().uses_native,
            workbench_on_rtg=self._workbench_on_rtg(),
            spare_files_folder=getattr(self, "detected",
                                       presets.Detected()).spare_folder,
            boot_options=options,
            kickstart_path=self.rom_row.path,
            kickstart_key=self.rom_key_row.path,
            wifi_ssid=self.ssid_row.get_text().strip(),
            wifi_password=self.psk_row.get_text(),
            wifi_country=self.country_row.get_text().strip() or "GB",
            expand_to_fill=(self.expand_row.get_active()
                            and mode is not builder.BuildMode.FRESH),
            extra_partitions=[row.spec() for row in self.extra_rows],
        )

    def _selected_adf_version(self) -> str:
        versions = getattr(self, "_adf_versions", [])
        index = self.os_version_row.get_selected()
        return versions[index] if index < len(versions) else ""

    # --------------------------------------------------------------- write

    def _on_write(self, _button) -> None:
        config = self.gather()
        problems = config.validate()
        if problems:
            self._toast(problems[0])
            return

        if config.target_is_device:
            device = next((d for d in self.device_list if d.path == config.target), None)
            body = (f"Everything on {config.target} will be destroyed.\n\n"
                    f"{device.description if device else config.target}")
            if device and device.mounted_paths:
                body += "\n\nCurrently mounted at: " + ", ".join(device.mounted_paths)
            if device and device.size > 512 * GIB:
                body += ("\n\nThis drive is unusually large for an SD card. "
                         "Check carefully that it is the right one.")
            dialog = Adw.AlertDialog(heading="Erase this card?", body=body)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("write", "Erase and write")
            dialog.set_response_appearance("write", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect("response", self._on_confirm, config)
            dialog.present(self)
        else:
            self._start(config)

    def _on_confirm(self, _dialog, response: str, config) -> None:
        if response == "write":
            self._start(config)

    def _start(self, config: builder.BuildConfig) -> None:
        self.cancel_flag.clear()
        self.log_buffer.set_text("")
        self.progress_bar.set_fraction(0.0)
        self.step_label.set_text("Preparing…")
        self.progress_title.set_subtitle(config.target)
        self.cancel_button.set_visible(True)
        self.back_button.set_visible(False)
        self.save_log_button.set_visible(False)
        self.outer.set_visible_child_name("progress")

        if config.target_is_device:
            threading.Thread(target=self._run_privileged, args=(config,),
                             daemon=True).start()
        else:
            threading.Thread(target=self._run_in_process, args=(config,),
                             daemon=True).start()

    def _progress(self) -> Progress:
        return Progress(
            on_step=lambda text: GLib.idle_add(self._set_step, text),
            on_fraction=lambda frac: GLib.idle_add(self._set_fraction, frac),
            on_log=lambda text: GLib.idle_add(self._append_log, text),
            cancelled=self.cancel_flag.is_set,
        )

    def _run_in_process(self, config: builder.BuildConfig) -> None:
        progress = self._progress()
        try:
            builder.run_build(config, progress)
        except Exception as error:  # noqa: BLE001 - surfaced in the log
            GLib.idle_add(self._finished, False, str(error))
            return
        GLib.idle_add(self._finished, True, "")

    def _run_privileged(self, config: builder.BuildConfig) -> None:
        """Stage downloads as the user, then write the card under pkexec."""
        progress = self._progress()
        try:
            staged = prepare.stage_emu68(config, progress)
            if staged is not None:
                config = dataclasses.replace(config, emu68_prepared_dir=str(staged))
            job = Path(GLib.get_user_runtime_dir() or "/tmp") / "pistorm-imager-job.json"
            jobs.save(config, job)
            os.chmod(job, 0o600)
        except Exception as error:  # noqa: BLE001
            GLib.idle_add(self._finished, False, str(error))
            return

        cli = Path(__file__).resolve().parent.parent / "cli.py"
        argv = ["pkexec", sys.executable, str(cli), "build",
                "--job", str(job), "--progress-json"]
        GLib.idle_add(self._append_log, "$ " + " ".join(argv))
        try:
            self.process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as error:
            GLib.idle_add(self._finished, False,
                          f"Could not start the privileged helper: {error}")
            return

        error_message = ""
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                GLib.idle_add(self._append_log, line)
                continue
            kind, value = event.get("type"), event.get("value")
            if kind == "step":
                GLib.idle_add(self._set_step, value)
            elif kind == "fraction":
                GLib.idle_add(self._set_fraction, value)
            elif kind == "log":
                GLib.idle_add(self._append_log, value)
            elif kind == "error":
                error_message = value
        code = self.process.wait()
        stderr = (self.process.stderr.read() if self.process.stderr else "").strip()
        self.process = None
        job.unlink(missing_ok=True)

        if code == 126:
            error_message = "Authentication was cancelled or denied."
        elif code and not error_message:
            error_message = stderr or f"The helper exited with status {code}."
        GLib.idle_add(self._finished, code == 0, error_message)

    def _on_cancel(self, _button) -> None:
        self.cancel_flag.set()
        self._append_log("Cancelling…")
        if self.process is not None:
            self.process.terminate()

    def _set_step(self, text: str) -> bool:
        self.step_label.set_text(text)
        return False

    def _set_fraction(self, fraction: float) -> bool:
        self.progress_bar.set_fraction(fraction)
        self.progress_bar.set_text(f"{fraction * 100:.0f}%")
        return False

    def _append_log(self, text: str) -> bool:
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, text + "\n")
        mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self.log_view.scroll_mark_onscreen(mark)
        self.log_buffer.delete_mark(mark)
        return False

    def _on_close(self, _window) -> bool:
        self._remember_session()
        return False

    def _finished(self, success: bool, message: str) -> bool:
        self._remember_session()
        self.cancel_button.set_visible(False)
        self.back_button.set_visible(True)
        self.save_log_button.set_visible(True)
        if success:
            self.step_label.set_text("Finished - the card is ready")
            self._set_fraction(1.0)
            self._append_log("Done. Eject the card and put it in your PiStorm.")
            self._toast("Card written successfully")
        else:
            self.step_label.set_text("Failed")
            self._append_log(f"ERROR: {message}")
            self._toast("The build failed - see the log")
        return False

    # ------------------------------------------------------ saved sessions

    def interface_state(self) -> dict:
        """The choices a BuildConfig cannot express, so they can be restored.

        Only those. Anything the configuration already carries - the target,
        the card size - must not be written here as well: the quick screen
        keeps its own copy of both, that copy goes stale the moment either is
        set on its own page, and this state is applied after the
        configuration, so the stale copy is the one that wins.
        """
        return {
            "machine": self._machine().key,
            "display": self._display().name,
            "workbench_screen": "rtg" if self._prefer_rtg_screen() else "native",
            "primary_source": self._primary(),
            "system_source": self._system_source(),
            "pimiga_folder": self.quick_pimiga.path,
            "hdf_source": self.quick_hdf.path,
            "pfs3_handler": self.quick_donor.path,
            "kickstart": self.rom_row.path,
            "kickstart_key": self.rom_key_row.path,
            "adf_folder": self.adf_row.path,
            "trapdoor": self.quick_trapdoor.get_active(),
            "system_size": self.quick_system.get_text(),
            "boot_size": self.boot_size_row.get_text(),
        }

    def apply_interface_state(self, state: dict) -> None:
        if not state:
            return
        was_ready, self._ready = self._ready, False
        try:
            for index, machine in enumerate(machines.MACHINES):
                if machine.key == state.get("machine"):
                    self.quick_machine.set_selected(index)
            for index, display in enumerate(machines.Display):
                if display.name == state.get("display"):
                    self.quick_display.set_selected(index)
            self.quick_workbench_screen.set_selected(
                1 if state.get("workbench_screen") == "native" else 0)
            #  Older sessions stored the combo position, which no longer means
            #  the same thing; only a recognised name is honoured.
            saved_source = state.get("system_source")
            saved_primary = state.get("primary_source")
            if saved_primary not in PRIMARY_SOURCES:
                #  Sessions saved before the two were separated recorded only
                #  the system source, which still says which one was primary.
                saved_primary = (saved_source
                                 if saved_source in PRIMARY_SOURCES
                                 else "default")
            self.quick_primary.set_selected(
                PRIMARY_SOURCES.index(saved_primary))
            self.quick_system_source.set_selected(
                FRESH_SOURCES.index(saved_source)
                if saved_source in FRESH_SOURCES else 0)
            self.quick_pimiga.set_path(state.get("pimiga_folder", ""))
            self.quick_hdf.set_path(state.get("hdf_source", ""))
            self.quick_donor.set_path(state.get("pfs3_handler", ""))
            self.rom_row.set_path(state.get("kickstart", ""))
            self.rom_key_row.set_path(state.get("kickstart_key", ""))
            self.adf_row.set_path(state.get("adf_folder", ""))
            self.quick_trapdoor.set_active(bool(state.get("trapdoor")))
            for row, key in ((self.quick_system, "system_size"),
                             (self.boot_size_row, "boot_size")):
                if state.get(key):
                    row.set_text(str(state[key]))
            #  The target and the card size come from the configuration, which
            #  apply() has already put in place.  Sessions saved before this
            #  also carry them here, and honouring those would undo it: a card
            #  built to a 125 GiB image came back as a 59 GiB SD card.
        finally:
            self._ready = was_ready
        self._on_machine_changed()
        self._mirror_target()
        self._on_quick_hdf()

    def _restore_session(self) -> None:
        """Pick up where the last session left off, if there was one."""
        if not jobs.have_session():
            return
        try:
            config, state, reduced = jobs.load_session()
        except Exception as error:  # noqa: BLE001 - a stale file is not fatal
            self._append_log(f"Could not restore the last session: {error}")
            return
        try:
            self._apply_saved(config, state)
        except Exception as error:  # noqa: BLE001
            self._toast(f"Could not restore the last session: {error}")
            return

        if reduced:
            self._toast("Restored your last setup, but its partition layout was "
                        "saved by an older version and has been reset - apply "
                        "the quick setup again")
            return
        #  A card that is no longer plugged in cannot be the target; fall back
        #  to an image file rather than leaving nothing selected.
        if self.quick_target.get_selected() == 0 and not self.device_list:
            self.quick_target.set_selected(1)
            self._mirror_target()
            self._toast("Restored your last setup - the card it used is not "
                        "connected, so an image file is selected instead")
            return
        self._toast("Restored your last setup")

    def _remember_session(self) -> None:
        try:
            jobs.save_session(self.gather(), self.interface_state())
        except Exception:  # noqa: BLE001 - never block quitting over this
            pass

    # -------------------------------------------------------- menu actions

    def _on_save_settings(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Save settings", initial_name="pistorm.json")

        def done(dlg, result) -> None:
            try:
                file = dlg.save_finish(result)
            except Exception:  # noqa: BLE001
                return
            try:
                jobs.save_session(self.gather(), self.interface_state(),
                                  file.get_path())
                self._toast("Settings saved")
            except Exception as error:  # noqa: BLE001
                self._toast(f"Could not save: {error}")

        dialog.save(self, None, done)

    def _on_load_settings(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Load settings")

        def done(dlg, result) -> None:
            try:
                file = dlg.open_finish(result)
            except Exception:  # noqa: BLE001
                return
            try:
                config, state, _reduced = jobs.load_session(file.get_path())
                self._apply_saved(config, state)
                self._toast("Settings loaded")
            except Exception as error:  # noqa: BLE001
                self._toast(f"Could not load: {error}")

        dialog.open(self, None, done)

    def _on_forget_session(self, _button) -> None:
        """Stop restoring the last setup, for when it is no longer wanted."""
        try:
            jobs.session_file().unlink(missing_ok=True)
            self._toast("The saved setup has been forgotten")
        except OSError as error:
            self._toast(f"Could not remove it: {error}")

    def _on_inspect(self, _button) -> None:
        try:
            config = self.gather()
        except Exception as error:  # noqa: BLE001
            self._toast(str(error))
            return
        config = dataclasses.replace(config, mode=builder.BuildMode.CUSTOMISE)
        text = builder.describe_target(config)
        dialog = Adw.AlertDialog(heading="Target contents", body=text)
        dialog.add_response("ok", "Close")
        dialog.present(self)

    def _on_save_log(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Save log", initial_name="pistorm-imager.log")

        def done(dlg, result) -> None:
            try:
                file = dlg.save_finish(result)
            except Exception:  # noqa: BLE001
                return
            start, end = self.log_buffer.get_bounds()
            text = self.log_buffer.get_text(start, end, False)
            Path(file.get_path()).write_text(text, encoding="utf-8")
            self._toast("Log saved")

        dialog.save(self, None, done)

    def _on_check_updates(self, _button) -> None:
        """Ask GitHub whether there is a newer release, off the UI thread."""
        self._toast("Checking for updates…")

        def work() -> None:
            release = updates.latest()
            GLib.idle_add(self._updates_answered, release)

        threading.Thread(target=work, daemon=True).start()

    def _updates_answered(self, release) -> None:
        if release is None:
            self._update_dialog(
                "Could not check for updates",
                "GitHub could not be reached, or it has no published releases "
                "yet. Nothing is wrong with this copy - the question simply "
                "could not be answered.", None)
            return False
        if not updates.is_newer(release.tag):
            self._update_dialog(
                "No newer version available",
                f"This is version {__version__}, and {release.name} is the "
                f"newest release. You are up to date.", None)
            return False
        notes = release.notes or "No release notes were published."
        if len(notes) > 2000:
            notes = notes[:2000].rstrip() + "\n\n(continues on GitHub)"
        self._update_dialog(
            f"{release.name} is available",
            f"You have version {__version__}.\n\n{notes}", release.url)
        return False

    def _update_dialog(self, heading: str, body: str, url: str | None) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", "Close")
        if url:
            dialog.add_response("open", "Go to GitHub")
            dialog.set_response_appearance("open",
                                           Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", lambda _d, name, link=url:
                           Gtk.UriLauncher(uri=link).launch(self, None, None)
                           if name == "open" else None)
        dialog.set_default_response("close")
        dialog.present(self)

    def _on_about(self, _button) -> None:
        about = Adw.AboutDialog(
            application_name="PiStorm Imager",
            application_icon="drive-removable-media",
            developer_name="PiStorm Imager for Linux",
            version=__version__,
            comments=("Prepare an SD card for PiStorm and Emu68 on Linux: build a "
                      "new card, write a pre-built image such as PiMiga, or refresh "
                      "the boot partition of a card you already have."),
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self)

    # ------------------------------------------------------ applying config

    def apply(self, config: builder.BuildConfig, *,
              keep_partitions: bool = False) -> None:
        """Push a loaded BuildConfig back into the widgets."""
        was_ready, self._ready = self._ready, False
        for index, (_label, mode, _hint) in enumerate(MODES):
            if mode is config.mode:
                self.mode_row.set_selected(index)
        for index, variant in enumerate(emu68.VARIANTS):
            if variant.key == config.variant:
                self.variant_row.set_selected(index)
        self.install_emu_row.set_active(config.install_emu68)
        self.image_row.set_path(config.source_image)
        self.hdf_row.set_path(config.hdf_image)
        self.repair_row.set_active(config.repair_rdb)
        self.local_zip_row.set_path(config.emu68_archive)
        self.rom_row.set_path(config.kickstart_path)
        self.rom_key_row.set_path(config.kickstart_key)
        self.volume_row.set_text(config.amiga_volume_name)
        self.adf_row.set_path(config.adf_folder)
        #  The operating system combo is the only place this is recorded now,
        #  and it drives the partition layout, so it has to say where the system
        #  actually came from - not merely whether floppies were involved.
        source = getattr(config, "system_source", "")
        if source not in SYSTEM_SOURCES or source == "auto":
            source = "adf" if config.install_amigaos else "none"
        self.quick_primary.set_selected(
            PRIMARY_SOURCES.index(source) if source in PRIMARY_SOURCES else 0)
        self.quick_system_source.set_selected(
            FRESH_SOURCES.index(source) if source in FRESH_SOURCES else 0)
        if config.adf_version:
            self._pending_adf_version = config.adf_version
        self.ssid_row.set_text(config.wifi_ssid)
        self.psk_row.set_text(config.wifi_password)
        self.country_row.set_text(config.wifi_country)
        self.boot_size_row.set_text(human_size(config.boot_size).replace(" MiB", "M")
                                    .replace(" GiB", "G"))

        for row in list(self.partition_rows):
            self.partition_group.remove(row)
        self.partition_rows.clear()
        for spec in config.amiga_partitions:
            self._add_partition(spec)
        if not self.partition_rows:
            self._add_partition()
        #  What the quick settings *would* have produced, not what was just
        #  loaded.  The relayout tells a layout somebody arranged from one it
        #  derived itself by comparing the rows against this, so recording the
        #  loaded rows here told it they were its own to redraw - and four
        #  saved drives came back as the generic layout.
        self._derived_partitions = (
            self._quick_layout() if keep_partitions
            else [row.spec() for row in self.partition_rows])

        options = config.boot_options
        for index, (_label, group, mode_id) in enumerate(bootcfg.HDMI_MODES):
            if group == options.hdmi_group and mode_id == options.hdmi_mode:
                self.hdmi_row.set_selected(index)
                break
        self.overclock_row.set_selected({None: 0, True: 1, False: 2}[options.overclock])
        self.antenna_row.set_selected(
            {None: 0, True: 1, False: 2}[options.cm4_external_antenna])
        self.vc4_row.set_value(options.vc4_mem or 0)
        self.vbr_row.set_active(options.vbr_move)
        self.slowdown_row.set_active(options.chip_slowdown)
        self.dbf_row.set_active(options.dbf_slowdown)
        self.blitwait_row.set_active(options.blitwait)
        self.swapdf_row.set_active(options.swap_df0_with_df1)
        self.unit0_row.set_active(options.sd_unit0_rw)
        self.extra_row.set_text(options.extra_cmdline)

        self.patch_display_row.set_active(config.patch_display)
        self.expand_row.set_active(config.expand_to_fill)
        for row in list(self.extra_rows):
            self.expand_group.remove(row)
        self.extra_rows.clear()
        for spec in config.extra_partitions:
            self._add_extra_partition(spec)
        if not self.extra_rows:
            self._add_extra_partition()
        self.target_row.set_selected(
            0 if config.target_is_device else (2 if config.output_hdf else 1))
        #  Quick setup holds its own copy of the target, and mirrors it onto
        #  this page; without updating it too, the next mirror would undo what
        #  has just been applied.
        self.quick_target.set_selected(0 if config.target_is_device else 1)
        if not config.target_is_device and config.target:
            self.quick_file.set_path(config.target)
        self.quick_card_size.set_text(exact_size_text(config.image_size))
        if not config.target_is_device:
            self.file_row.set_path(config.target)
        self._restore_package_choices(config)
        #  The list of Emu68 builds is fetched from GitHub in the background,
        #  so the one this setup was built against may not be offered yet.
        self._wanted_release = config.release_tag or ""
        self._populate_releases()
        self._ready = was_ready
        self._sync_visibility()

    def _apply_saved(self, config: builder.BuildConfig, state: dict) -> None:
        """Everything a loaded setup has to put back, in the order that works.

        Order is the whole of it. The interface state carries the machine and
        the display, which decide which software suits the card and which
        board the Source page shows, so both of those go back after it - and
        the configuration, not the state, is what says which they were.
        """
        self.apply(config, keep_partitions=True)
        self.apply_interface_state(state)
        self._restore_package_choices(config)
        self._restore_board(config)

    def _restore_board(self, config: builder.BuildConfig) -> None:
        """Put the board back after the machine has had its say.

        The Source page's board follows the model, which is right while the
        model is being chosen and wrong when a setup is being loaded: the
        machine arrives with the interface state, after the configuration, and
        set a PiStorm32-Lite card back to a plain PiStorm without a word.
        """
        for index, variant in enumerate(emu68.VARIANTS):
            if variant.key == config.variant:
                self.variant_row.set_selected(index)
                return

    def _restore_package_choices(self, config: builder.BuildConfig) -> None:
        """Put the software choices, and where they come from, back.

        gather() has always saved these; nothing ever put them back, so
        loading a setup returned a card with the donor forgotten and every
        tick cleared, however carefully the list had been chosen.
        """
        if config.package_donor:
            self.package_donor.set_path(config.package_donor)
        #  The rows have to be worked out against this donor before they can
        #  be ticked: refreshing afterwards would clear anything it thought
        #  unusable, including choices that are perfectly usable.
        self._refresh_packages()
        wanted = set(config.package_keys)
        for key, row in self.package_rows.items():
            if row.get_sensitive() or key in wanted:
                row.set_active(key in wanted)
        self._tick_what_is_needed()
