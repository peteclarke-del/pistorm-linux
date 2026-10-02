"""The main PiStorm Imager window."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .. import APPLICATION_NAME, __version__  # noqa: E402
from ..core import (amigacd, amigaos, boingbag, bootaddon, bootcfg,  # noqa: E402
                    builder, content, devices,
                    distributions,
                    emu68, hdfcheck, jobs, kickstart, machines, packages,
                    prepare, presets)
from ..core.util import (GIB, Progress, describe_size,  # noqa: E402
                         exact_size_text, human_size,  # noqa: E402
                         parse_size)
from .app_updater import AppUpdateControls, AppUpdater, attach_to_about  # noqa: E402
from .widgets import (FileRow, PackageCheck, SaveRow, combo,  # noqa: E402
                      show_full_value)

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
FRESH_SOURCES = ["adf", "none", "cd"]

MODES = [
    ("Build a new card", builder.BuildMode.FRESH,
     "Partition the card yourself and fill the drives: Workbench from floppy "
     "images, the files out of somebody's drive image or folder, and the "
     "software you choose. This is the one that can combine them."),
    ("Write a pre-built image", builder.BuildMode.IMAGE,
     "Write PiMiga, an Emu68 Hatcher image or a backup of your own card, then "
     "apply your Emu68 build and settings on top."),
    ("Write a drive image unchanged", builder.BuildMode.HDF,
     "Put a WinUAE/FS-UAE/HstWB .hdf on the card exactly as it is, keeping its "
     "own partitions and file systems, with an Emu68 boot partition built "
     "around it. Nothing can be added to it and nothing resized - to take the "
     "files off a drive image and put them on a card of your own layout, use "
     "Build a new card and give a partition that image as its contents."),
    ("Update an existing card", builder.BuildMode.CUSTOMISE,
     "Leave everything on the card alone and only refresh the boot partition."),
    ("Rebuild one drive on a card", builder.BuildMode.REWRITE,
     "Format one Amiga drive on a card or image that already exists and fill "
     "it again - a System drive with a new Workbench and software, say - "
     "leaving the partition table, the boot partition and every other drive "
     "exactly as they are."),
    ("Export drives as .hdf", builder.BuildMode.EXPORT,
     "Read the Amiga drives back out of a card or an image and write each one "
     "as its own .hdf, ready to mount in WinUAE or FS-UAE. Each file carries "
     "its own Rigid Disk Block and the file system handler the card embedded, "
     "so a PFS3 drive is readable with nothing else supplied."),
]

FILESYSTEMS = ["PFS3", "PDS3", "FFS-INTL", "FFS", "SFS"]

#  The steps a task can take, as the pages that hold them: the stack name,
#  then the title and icon the switcher shows.
STEPS = {
    "amiga": ("Machine", "computer-symbolic"),
    "source": ("System", "folder-download-symbolic"),
    "storage": ("Drives", "drive-harddisk-symbolic"),
    "packages": ("Software", "package-x-generic-symbolic"),
    "options": ("Emu68", "preferences-system-symbolic"),
    "target": ("Target", "media-flash-symbolic"),
    "review": ("Review", "object-select-symbolic"),
    "export": ("Export", "document-save-symbolic"),
}

#  What can be done, and the steps each takes, in the order their choices
#  gate one another. The machine comes first wherever there is one to ask
#  about, because it decides what every later step can offer: the board, the
#  display, which software suits it. Where the task starts from a card that
#  already exists, the card comes first, because it decides what there is to
#  change. The task is chosen once, on the first screen; changing it means
#  going back there, so nothing chosen for one task is left standing in
#  another.
TASKS = [
    (builder.Task.NEW_CARD, "A new PiStorm card",
     "Emu68 on the boot partition and Amiga drives beside it, with AmigaOS "
     "and the software you choose.",
     "media-flash-symbolic",
     ("amiga", "source", "storage", "packages", "options", "target",
      "review")),
    (builder.Task.SPLIT, "A PiStorm with its drives elsewhere",
     "Workbench and your software on a CF card or disk for the IDE port, "
     "and Emu68 on the Pi's own boot card - both written from one set of "
     "choices.",
     "drive-multidisk-symbolic",
     ("amiga", "source", "storage", "packages", "options", "target",
      "review")),
    (builder.Task.BOOT_CARD, "A PiStorm boot card only",
     "Emu68 and its settings and nothing else, for a machine whose Amiga "
     "drives are on a CF card or a disk on its own IDE port.",
     "media-removable-symbolic",
     ("amiga", "options", "target", "review")),
    (builder.Task.AMIGA_DRIVE, "A drive for the Amiga's IDE or SCSI port",
     "Amiga drives with no boot partition: a CF card or disk for the IDE "
     "port, behind a PiStorm that boots from its own card or a real "
     "accelerator.",
     "drive-harddisk-symbolic",
     ("amiga", "source", "storage", "packages", "target", "review")),
    #  One task for any image: a whole card and an Amiga drive are written
    #  differently, but which one a file is the file itself says, so the
    #  task becomes Task.DRIVE_IMAGE once a drive is chosen.
    (builder.Task.PREPARED, "Write an image to a card",
     "A finished card - CaffeineOS, an Emu68 Hatcher image, a backup - or "
     "a WinUAE, FS-UAE or HstWB drive image, with your Emu68 and settings "
     "applied.",
     "folder-download-symbolic",
     ("amiga", "source", "storage", "options", "target", "review")),
    (builder.Task.REBUILD, "Rebuild one drive",
     "Format and fill one drive on a card you already have - a new System "
     "with the software you choose - without writing the drives beside it "
     "again.",
     "view-refresh-symbolic",
     ("target", "amiga", "source", "packages", "review")),
    (builder.Task.UPDATE, "Update an existing card",
     "Only the boot partition: a newer Emu68, another Kickstart, different "
     "settings. The Amiga drives are left alone.",
     "emblem-synchronizing-symbolic",
     ("target", "amiga", "options", "review")),
    (builder.Task.EXPORT, "Export drives as .hdf",
     "Take the Amiga drives out of a card or an image and write each one as "
     "its own file, ready for WinUAE or FS-UAE.",
     "document-save-symbolic",
     ("export",)),
]
TASK_STEPS = {task: steps for task, _t, _s, _i, steps in TASKS}
TASK_STEPS[builder.Task.DRIVE_IMAGE] = TASK_STEPS[builder.Task.PREPARED]
#  The tasks a chosen image can turn into: a card or a drive.
IMAGE_TASKS = {builder.ImageKind.CARD: builder.Task.PREPARED,
               builder.ImageKind.DRIVE: builder.Task.DRIVE_IMAGE}

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
    "release_tag", "kernel_key", "emu68_archive", "install_emu68",
    "kickstart_key",
    "amiga_volume_name", "wifi_ssid", "wifi_password", "wifi_country",
    "expand_to_fill", "extra_partitions", "boot_only",
    #  The quick page has a source chooser of its own, so these belong to the
    #  Source page alone: applying a fresh layout used to empty it.
    "source_image", "hdf_image", "repair_rdb",
    #  What is providing the processor, and the CD install, are decided by a
    #  person rather than by the machine or the layout, so a quick setup has
    #  no opinion about them and must not throw them away.
    "accelerator", "accelerator_cpu", "amiga_only",
    "os_cd", "os_cd_release", "os_cd_options", "boingbag_archives",
    "boingbags", "boingbag_emulator",
    #  Which Raspberry Pi is plugged into the board, which of its USB sockets
    #  the Amiga was given, how much chip RAM is fitted and what is going onto
    #  the boot partition are facts about somebody's hardware and their
    #  choice. A quick setup knows the Amiga, not what has been added to it.
    "pi_model", "usb_port", "chip_ram", "boot_addons",
)

#  The same for the boot settings.  The machine decides the ones that follow
#  from its chipset and its display; these are the ones only a person can.
KEPT_BOOT_OPTIONS = (
    "overclock", "cm4_external_antenna", "swap_df0_with_df1", "sd_unit0_rw",
    "hdmi_force_hotplug", "boot_delay", "gpu_mem", "total_mem", "limit_2g",
    "z2_ram_size", "unicam_extra",
    #  Emu68 1.1 settings. The machine has no opinion about any of them: an
    #  IDE port with nothing on it, a machine whose Agnus disagrees with its
    #  owner, and how much cache to give the translator are all things only
    #  the person in front of it knows.
    "no_ide", "video_standard", "jit_cache_mb",
    #  Follows the USB socket, which the quick setup keeps, so the line that
    #  makes that socket work has to be kept with it.
    "otg_mode",
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
        #  An image was offered nothing to leave out, because this only ever
        #  listed a host directory - so a drive imported from an .hdf could
        #  be taken whole or not at all.
        found = self._what_is_in_there(path)
        if not path:
            self.exclude_group.set_subtitle("Choose a folder or image to see "
                                            "what it holds")
        elif not found:
            self.exclude_group.set_subtitle("Nothing in here can be listed "
                                            "separately")
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
                f"{len(found)} item(s), {len(unsuitable)} of them not for "
                f"this machine")

    def _what_is_in_there(self, path: str) -> list:
        """What can be left out of this source, folder or image alike.

        Remembered per (path, drive). Rebuilding the category list walks the
        whole volume, and every signal that could change the list rebuilt it -
        so choosing one image walked it several times over before the window
        had even appeared. Neither the file nor the drive inside it changes
        while the application is looking at it.
        """
        if not path:
            return []
        key = (path, self._chosen_drive())
        cached = getattr(self, "_contents_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        found = self._discover_contents(path)
        self._contents_cache = (key, found)
        return found

    def _discover_contents(self, path: str) -> list:
        if Path(path).is_dir():
            return content.discover(path)
        try:
            reader, _label = amigaos.open_amiga_volume(path,
                                                       self._chosen_drive())
        except Exception:                        # noqa: BLE001 - unreadable
            return []
        try:
            return content.discover_volume(reader)
        finally:
            try:
                reader.f.close()
            except Exception:                    # noqa: BLE001
                pass

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


def _version(pair) -> str:
    """A (version, revision) pair as an Amiga would write it."""
    return f"{pair[0]}.{pair[1]}" if pair else "an unreadable version"


class ImagerWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application, title=APPLICATION_NAME,
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
        #  One for the life of the window, so the answer to a check is still
        #  there when the About dialog is opened again.
        self.app_updater = AppUpdater()
        self.about_dialog: Adw.AboutDialog | None = None

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.outer = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.outer)
        self.outer.add_named(self._build_setup(), "setup")
        #  The build log used to be a page of this same window, so it inherited
        #  whatever size the setup pages wanted and had to be resized by hand
        #  every time. It is a window of its own now, sized for reading a log.
        self.progress_window = Adw.Window(
            modal=True, transient_for=self, hide_on_close=True,
            default_width=900, default_height=720, title="Writing")
        self.progress_window.set_content(self._build_progress())
        self.progress_window.connect("close-request", self._on_progress_close)

        #  Long values - a disk description, a screen mode, a board name - are
        #  ellipsised in a combo row's value slot; show them in full instead.
        show_full_value(
            self.mode_row, self.variant_row, self.release_row,
            self.quick_machine, self.quick_display, self.quick_system_source,
            self.quick_primary,
            self.hdmi_row,
            self.overclock_row, self.antenna_row, self.target_row,
            self.device_row, self.os_version_row,
            #  The split build's drives target was added without this, and
            #  its card names were cut off before the part that tells two
            #  USB readers apart.
            self.drives_kind_row, self.drives_device_row,
            self.rewrite_drive_row, self.kernel_row, self.usb_port_row,
            self.video_row, self.quick_pi, self.quick_workbench_screen,
            self.quick_chip_ram, self.quick_accelerator,
            self.quick_accelerator_cpu,
        )
        self._ready = True
        self._refresh_packages()
        self._target_settled()
        self._on_machine_changed()
        self._detect_material()
        self._refresh_devices()
        self._restore_session()
        self.connect("close-request", self._on_close)
        #  Saved at every step too, not only on closing: a window that hung
        #  and had to be killed took everything chosen in it with it.
        #  Connected after the restore, so a half-applied setup is never
        #  saved over the one being restored.
        self.stack.connect("notify::visible-child-name",
                           lambda *_a: self._task is not None
                           and self._remember_session())
        self._load_releases_async()
        self._sync_visibility()
        #  Always on the choice of task. A restored session brings back what
        #  was chosen, and choosing the same task again picks it up.
        self._leave_task()
        self._settled = True

    # ------------------------------------------------------------ setup UI

    def _build_setup(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = self.switcher = Adw.ViewSwitcher(stack=self.stack,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        #  A real menu model rather than a popover full of buttons: this is
        #  what closes itself when an item is chosen, and it brings keyboard
        #  navigation and the platform's own styling with it.
        entries = (("Save settings…", "save-settings", self._on_save_settings),
                   ("Load settings…", "load-settings", self._on_load_settings),
                   ("Forget saved setup", "forget-session", self._on_forget_session),
                   ("Inspect the target", "inspect-target", self._on_inspect),
                   ("Check for Application Updates…", "check-updates",
                    self._on_check_updates),
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

        self.stack.add_titled_with_icon(self._page_quick(), "quick", "Start",
                                        "go-home-symbolic")
        #  Built in the order their widgets depend on one another; shown in
        #  the order the chosen task takes them - see _show_steps.
        self._step_pages = {
            "source": self._page_source(),
            "amiga": self._page_amiga(),
            "storage": self._page_storage(),
            "packages": self._page_packages(),
            "options": self._page_options(),
            "target": self._page_target(),
            "review": self._page_review(),
            "export": self._page_export(),
        }
        for name in STEPS:
            title, icon = STEPS[name]
            self.stack.add_titled_with_icon(self._step_pages[name], name,
                                            title, icon)
        #  What goes on the boot drive is the System step's question, after
        #  the source it comes from.
        self._move_group(self.os_group, self.page_source)
        self._move_group(self.os_cd_group, self.page_source)
        view.set_content(self.stack)

        #  Nothing is chosen until a task is: the first screen is the choice.
        self._customising = False
        self._task: builder.Task | None = None
        self._steps: tuple[str, ...] = ()
        self.stack.connect("notify::visible-child-name",
                           lambda *_a: self._update_navigation())

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                         margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        #  Back sits with Write, at the other end of the same bar: they are
        #  the two things you do when you have finished reading the page.
        #  Centred, at their own height: the summary beside them can run to
        #  several lines, and buttons left to fill the bar swelled with it.
        self.back_button = Gtk.Button(label="Back", valign=Gtk.Align.CENTER)
        self.back_button.add_css_class("pill")
        self.back_button.connect("clicked", lambda _b: self._go_back())
        bottom.append(self.back_button)
        #  Two lines at most, the rest in its tooltip: every concern a build
        #  raised made the bar taller, until it covered the page above it.
        self.summary = Gtk.Label(xalign=0.0, wrap=True, hexpand=True,
                                 lines=2, ellipsize=Pango.EllipsizeMode.END)
        self.summary.add_css_class("dim-label")
        self.summary.connect(
            "notify::label",
            lambda label, _p: label.set_tooltip_text(label.get_text() or None))
        bottom.append(self.summary)
        #  Front to back: Next takes the task's steps in their order, and
        #  Write is only offered on the last of them, once everything before
        #  it has been seen.
        self.next_button = Gtk.Button(label="Next", valign=Gtk.Align.CENTER)
        self.next_button.add_css_class("suggested-action")
        self.next_button.add_css_class("pill")
        self.next_button.connect("clicked", lambda _b: self._go_next())
        bottom.append(self.next_button)
        self.write_button = Gtk.Button(label="Write card",
                                       valign=Gtk.Align.CENTER)
        self.write_button.add_css_class("suggested-action")
        self.write_button.add_css_class("pill")
        self.write_button.connect("clicked", self._on_write)
        bottom.append(self.write_button)
        self.bottom_bar = bottom
        view.add_bottom_bar(bottom)
        return view

    def _go_back(self) -> None:
        """One step back along the task, or to the choice of task.

        Going back to the first screen leaves the task: what was chosen for
        it stays in the widgets, but nothing is offered from it until a task
        is chosen again, and that task decides afresh what applies.
        """
        steps = self._steps
        current = self.stack.get_visible_child_name()
        if current in steps and steps.index(current) > 0:
            self.stack.set_visible_child_name(
                steps[steps.index(current) - 1])
            return
        self._leave_task()

    def _go_next(self) -> None:
        steps = self._steps
        current = self.stack.get_visible_child_name()
        if current in steps and steps.index(current) < len(steps) - 1:
            self.stack.set_visible_child_name(
                steps[steps.index(current) + 1])

    def _update_back(self) -> None:
        """The bottom bar belongs to a task; the first screen has none."""
        self._update_navigation()

    def _update_navigation(self) -> None:
        """Back, Next and Write, for where in the task the window is."""
        if not hasattr(self, "next_button"):
            return
        in_task = self._task is not None
        if hasattr(self, "bottom_bar"):
            self.bottom_bar.set_visible(in_task)
        current = self.stack.get_visible_child_name()
        last = bool(self._steps) and current == self._steps[-1]
        self.next_button.set_visible(in_task and not last)
        self.write_button.set_visible(in_task and last)

    def _start_task(self, task: builder.Task) -> None:
        """Take up a task: it decides the card's shape and the steps shown.

        The four settings the task decides - the build mode, Emu68 only,
        drives only, and whether Emu68 goes on - are set here and nowhere
        else: none of them is shown as a switch that a later page could
        turn into a card that cannot work.
        """
        self._task = task
        self._customising = True
        was, self._ready = self._ready, False
        try:
            for index, entry in enumerate(MODES):
                if entry[1] is task.mode:
                    self.mode_row.set_selected(index)
                    break
            self.boot_only_row.set_active(task is builder.Task.BOOT_CARD)
            self.amiga_only_row.set_active(task is builder.Task.AMIGA_DRIVE)
            if task.emu68 is not None:
                self.install_emu_row.set_active(task.emu68)
            #  A task that writes Emu68 is for a PiStorm, whatever the
            #  processor row was last left saying.
            if task.writes_boot_partition and task is not builder.Task.EXPORT:
                self.quick_accelerator.set_selected(
                    list(machines.Accelerator).index(
                        machines.Accelerator.PISTORM))
        finally:
            self._ready = was
        #  An image already chosen decides between a card and a drive again,
        #  rather than the tile's choice standing over what the file says.
        if task in IMAGE_TASKS.values() and self.image_row.path:
            self._on_image_chosen()
        self._suggest_what_was_found()
        self._show_steps(TASK_STEPS[self._task])
        self._on_accelerator_changed()
        self._sync_visibility()
        self._relayout_partitions()
        self._update_summary()

    def _leave_task(self) -> None:
        self._task = None
        self._customising = False
        self._show_steps(())

    def _show_steps(self, steps: tuple[str, ...]) -> None:
        """Show this task's steps, in its order, and only those.

        A switcher lists its pages in the order they were added, so the pages
        are taken off and put back in the order the task wants: Rebuild
        starts from the card, a new card from the machine.
        """
        self._steps = tuple(steps)
        for name in STEPS:
            child = self.stack.get_child_by_name(name)
            if child is not None:
                self.stack.remove(child)
        order = list(steps) + [name for name in STEPS if name not in steps]
        for name in order:
            title, icon = STEPS[name]
            page = self.stack.add_titled_with_icon(self._step_pages[name],
                                                   name, title, icon)
            page.set_visible(name in steps)
        quick = self.stack.get_page(self.stack.get_child_by_name("quick"))
        quick.set_visible(not steps)
        #  Seven steps do not fit side by side with their names; stacked,
        #  icon over name, they do.
        self.switcher.set_policy(Adw.ViewSwitcherPolicy.NARROW
                                 if len(steps) > 5
                                 else Adw.ViewSwitcherPolicy.WIDE)
        self.stack.set_visible_child_name(steps[0] if steps else "quick")
        self._update_navigation()

    def _suggest_what_was_found(self) -> None:
        """Put what was found on this machine where the task will ask for it.

        Only into empty choosers, so nothing anybody chose is overwritten:
        the Kickstart and the Workbench disks found at startup used to be
        shown on the first screen and put nowhere a build would read them.
        """
        detected = getattr(self, "detected", None)
        if detected is None:
            return
        if detected.kickstart is not None and not self.rom_row.path:
            self.rom_row.set_path(str(detected.kickstart.path))
            self._on_rom_chosen()
        if detected.adf_folder and not self.adf_row.path:
            self.adf_row.set_path(str(detected.adf_folder))
            self._scan_adfs()

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

    def _set_customising(self, on: bool) -> None:
        """Into the task the settings describe, or back to the choice.

        Kept for what calls it: a restored session and the tests. On takes
        up the task the current settings make - or a new card, if they make
        none - and off leaves it.
        """
        if not on:
            self._leave_task()
            return
        try:
            task = self.gather().task
        except Exception:                        # noqa: BLE001 - no target
            task = None
        self._start_task(task or self._task or builder.Task.NEW_CARD)

    def _page_quick(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        self.page_quick = page


        #  A masthead, so the choice sits in the window rather than clinging
        #  to the top of it. The page was three rows and then a great deal of
        #  nothing, which read as though something had failed to load.
        banner = Adw.PreferencesGroup()
        #  Kept compact so the whole grid of tasks fits on a small screen
        #  without scrolling.
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       halign=Gtk.Align.CENTER)
        hero.set_margin_top(0)
        hero.set_margin_bottom(0)
        icon = Gtk.Image.new_from_icon_name("pistorm-imager")
        icon.set_pixel_size(64)
        #  The installed application icon if the desktop has it, and a stock
        #  one if this is running from a checkout that has never installed it.
        if not Gtk.IconTheme.get_for_display(
                Gdk.Display.get_default()).has_icon("pistorm-imager"):
            icon.set_from_icon_name("drive-harddisk-symbolic")
        hero.append(icon)
        title = Gtk.Label(label=APPLICATION_NAME)
        title.add_css_class("title-1")
        hero.append(title)
        strap = Gtk.Label(
            label="Build an Amiga SD card for PiStorm and Emu68",
            wrap=True, justify=Gtk.Justification.CENTER)
        strap.add_css_class("dim-label")
        hero.append(strap)
        banner.add(hero)
        page.add(banner)

        #  The things anyone actually wants to do, rather than a page of
        #  settings that happens to be first.
        #  No description under the heading: each tile says what it does
        #  in its tooltip, and the line cost a row on a small screen.
        choices = Adw.PreferencesGroup(title="What would you like to do?")
        #  A grid of tiles, each one the whole of its task: a row apiece with
        #  a Start button at the end was a long column of identical buttons
        #  with the thing being chosen at the other side of the window.
        #  The name on the tile, what it does in its tooltip: with the whole
        #  description on each, eight of them ran four rows down the window.
        grid = Gtk.FlowBox(homogeneous=True, min_children_per_line=3,
                           max_children_per_line=3, column_spacing=12,
                           row_spacing=12,
                           selection_mode=Gtk.SelectionMode.NONE)
        self.task_tiles: dict[builder.Task, Gtk.Button] = {}
        for task, title_text, subtitle, icon_name, _steps in TASKS:
            tile = Gtk.Button(tooltip_text=subtitle)
            tile.add_css_class("card")
            tile.add_css_class("task-tile")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            for side in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{side}")(8)
            image = Gtk.Image.new_from_icon_name(icon_name)
            image.set_pixel_size(40)
            image.add_css_class("accent")
            box.append(image)
            heading = Gtk.Label(label=title_text, wrap=True,
                                justify=Gtk.Justification.CENTER)
            heading.add_css_class("heading")
            heading.set_max_width_chars(16)
            heading.set_valign(Gtk.Align.START)
            heading.set_vexpand(True)
            box.append(heading)
            tile.set_child(box)
            tile.connect("clicked", lambda _b, t=task: self._start_task(t))
            grid.append(tile)
            self.task_tiles[task] = tile
        choices.add(grid)
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
        #  What was found is put into the choosers when a task starts; this
        #  group is kept only so the detection has somewhere to report.
        self.group_detected = group

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
        #  Which Pi is on the board.  Nothing on a card used to depend on it -
        #  the boot partition carries a device tree for every model - so the
        #  question was never asked.  The USB controller is on the Pi, and a
        #  Pi 3 has none that anything here can drive, so software that uses
        #  one cannot be offered until this has been answered.
        self.quick_pi = Adw.ComboRow(
            title="Raspberry Pi on the board",
            subtitle="Only USB depends on this; everything else on the card "
                     "boots on any of them.",
            model=combo([pi.label for pi in machines.MACHINES[0].pi_models]))
        self.quick_pi.connect("notify::selected",
                              lambda *_a: self._on_pi_changed())
        group.add(self.quick_pi)
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
        #  How much chip RAM the Agnus can address.  Nothing needed this until
        #  an add-on that emulates a chipset did, and it is a fact about
        #  somebody's machine rather than anything derivable: an unexpanded
        #  A500 has 512K and the same board with an ACE2B has two megabytes.
        self.quick_chip_ram = Adw.ComboRow(
            title="Chip RAM fitted",
            subtitle="What the Agnus can address. Only the AGA add-on cares "
                     "about this; leave it alone unless you have upgraded it.",
            model=combo([machines.chip_ram_label(k)
                         for k in machines.MACHINES[0].chip_ram_options]))
        self.quick_chip_ram.connect("notify::selected",
                                    lambda *_a: self._on_chip_ram_changed())
        group.add(self.quick_chip_ram)
        #  What is actually executing 68k code.  A PiStorm clears every
        #  processor requirement AmigaOS has, but this tool can also build a
        #  drive for a machine that has not got one - so the question has to
        #  be askable rather than assumed.
        self.quick_accelerator = Adw.ComboRow(
            title="Processor",
            subtitle="AmigaOS 3.5 and 3.9 need a 68020 or better. Emu68 gives "
                     "a PiStorm a 68040, so a PiStorm can always run them; "
                     "3.2 runs on any processor.",
            model=combo([a.label for a in machines.Accelerator]))
        self.quick_accelerator.set_selected(
            list(machines.Accelerator).index(machines.Accelerator.PISTORM))
        self.quick_accelerator.connect(
            "notify::selected", lambda *_a: self._on_accelerator_changed())
        group.add(self.quick_accelerator)
        self.quick_accelerator_cpu = Adw.ComboRow(
            title="The accelerator's processor",
            model=combo([c.label for c in machines.Cpu]))
        self.quick_accelerator_cpu.set_selected(
            list(machines.Cpu).index(machines.Cpu.M68030))
        self.quick_accelerator_cpu.connect(
            "notify::selected", lambda *_a: self._on_layout_changed())
        self.quick_accelerator_cpu.set_visible(False)
        group.add(self.quick_accelerator_cpu)
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
                         "Don't install one - partition only",
                         f"Install AmigaOS {amigacd.release_names()} from a "
                         f"CD image"]))
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
        suggest = Gtk.Button(label="Use the suggested layout",
                             valign=Gtk.Align.CENTER)
        suggest.add_css_class("flat")
        suggest.connect("clicked", lambda _b: self._suggest_layout())
        group.set_header_suffix(suggest)
        #  Sizes are a storage question; the Storage page adds this.
        self.group_sizes = group


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
        #  Selectable, so it can be copied, but not focused when the page
        #  opens - it came up with every word highlighted.
        self.quick_plan.set_focusable(False)

        #  All one box.  A preferences group keeps plain widgets and rows in
        #  separate places, so adding the summary and then an ActionRow does
        #  not put the row after the summary - it puts it wherever the group
        #  keeps rows, which was above it.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("card")
        box.append(self.quick_plan)

        group.add(box)
        self.group_plan = group
        return page

    def _page_review(self) -> Adw.PreferencesPage:
        """The last step of every task: what it adds up to, before Write."""
        page = Adw.PreferencesPage()
        self.page_review = page
        self.missing_group = Adw.PreferencesGroup(
            title="Still needed",
            description="Write is offered once these are settled. Each is on "
                        "an earlier step.")
        self.missing_label = Gtk.Label(xalign=0.0, wrap=True,
                                       margin_top=6, margin_bottom=6,
                                       margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("card")
        box.append(self.missing_label)
        self.missing_group.add(box)
        page.add(self.missing_group)
        #  What will build but is probably not what was meant. These used to
        #  be appended to the bar at the bottom, which grew a line for each
        #  until it covered the page and swelled the buttons beside it.
        self.concerns_group = Adw.PreferencesGroup(
            title="Worth checking",
            description="The card will still be written; these are choices "
                        "that probably do not do what was meant.")
        self.concerns_label = Gtk.Label(xalign=0.0, wrap=True,
                                        margin_top=6, margin_bottom=6,
                                        margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("card")
        box.append(self.concerns_label)
        self.concerns_group.add(box)
        self.concerns_group.set_visible(False)
        page.add(self.concerns_group)
        page.add(self.group_plan)
        return page

    def _imported_drives(self) -> list[str]:
        """Every hard disk image whose files land on the bootable drive.

        The quick screen's chooser is not the only way in: the workflow fills
        DH0 from an image on the Storage page, and a drive imported that way
        needs the Workbench disks exactly as much.
        """
        paths = [self.quick_hdf.path] if self.quick_hdf.path else []
        for row in getattr(self, "partition_rows", []):
            try:
                spec = row.spec()
            except Exception:                    # noqa: BLE001 - half-typed row
                continue
            if spec.bootable and spec.content_hdf:
                paths.append(spec.content_hdf)
        return paths

    def _imported_needs_floppies(self) -> bool:
        """Whether the drive being imported brings no Workbench of its own."""
        for path in self._imported_drives():
            #  Asked on every redraw of the summary, and it reads the image
            #  each time; the answer only changes when the file does.
            try:
                stamp = (path, os.stat(path).st_mtime_ns)
            except OSError:
                continue
            cache = self.__dict__.setdefault("_floppy_need", {})
            if stamp not in cache:
                try:
                    found = presets.inspect_image_system(path)
                    #  Only when the drive was actually read and found to
                    #  bring no Workbench. An image this reader cannot open
                    #  says nothing either way, and treating that as "needs
                    #  the disks" would demand floppies for a perfectly good
                    #  drive on the strength of not having understood it.
                    cache[stamp] = bool(not found.error
                                        and found.needs_floppies)
                except Exception:                # noqa: BLE001 - not fatal
                    cache[stamp] = False
            if cache[stamp]:
                return True
        return False

    def _on_quick_hdf(self) -> None:
        path = self.quick_hdf.path
        if not path:
            self.quick_hdf_info.set_subtitle("No image selected")
            self._relayout_partitions()
            #  Both lists describe the drive that was chosen, so dropping the
            #  drive has to drop them: left standing, they would leave
            #  software out of a build that is no longer using that drive.
            self._refresh_older_copies()
            self._refresh_what_cannot_work()
            self._refresh_clutter()
            self._refresh_desktop()
            self._refresh_what_arrives()
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
        #  Whether the Workbench disks are needed follows from the drive that
        #  was just chosen, and only _sync_visibility reveals that chooser -
        #  so without this the option to add them never appeared.
        self._sync_visibility()
        self._relayout_partitions()
        self._refresh_older_copies()
        self._refresh_what_cannot_work()
        self._refresh_clutter()
        self._refresh_desktop()
        self._refresh_what_arrives()
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

    def _target_settled(self) -> None:
        """The Target page changed: what follows from where the result goes.

        There is one set of target controls. The quick start had a second,
        kept in step with these by mirroring both ways - and a mirror that
        wrote back as a side effect once deselected a card a signal after it
        was chosen, and an image file was written instead.
        """
        if getattr(self, "_settling_target", False) or not self._ready:
            return
        self._settling_target = True
        try:
            self._follow_the_card()
            self._show_size()
        finally:
            self._settling_target = False
        self._sync_visibility()
        self._relayout_partitions()

    def _follow_the_card(self) -> None:
        """Show the card's own size when writing to one, and lock the box.

        A size typed for a card is a guess at what the card holds, and the two
        meanings of "GB" make it a bad one: "125G" is 125 GiB, nine gigabytes
        more than a card sold as 125 GB. When there is a card in front of us
        its capacity is known exactly, so it is shown and the box is closed.

        **Exactly** is the word that matters. This wrote ``human_size`` into
        the box, which rounds to two decimals of a GiB - steps of 10.7 MB -
        and building an image file reads that text back. A 64 GB card holding
        63,864,569,856 bytes came back as "59.48 GiB", which is
        63,866,163,691: an image 1.6 MB too big for the card it was measured
        from, written and found not to fit. ``exact_size_text`` is the one
        that survives being read back, which is what it is for.
        """
        card = self._selected_device()
        for row in (self.file_size_row,):
            if card is not None and card.size:
                wanted = exact_size_text(card.size)
                if row.get_text() != wanted:
                    row.set_text(wanted)
                row.set_sensitive(False)
            else:
                #  Writing to an image file: the size is the user's to choose,
                #  and nothing else knows what card it is going onto. The box
                #  stayed locked from whenever a card was last selected, so a
                #  size that did not fit could not be corrected.
                row.set_sensitive(True)
        if card is not None and card.size:
            self.file_size_row.set_title(
                f"Card size - taken from {card.name}, which holds "
                f"{describe_size(card.size)}")
        else:
            self.file_size_row.set_title(
                "Image size - 32GB as cards are sold, 32GiB binary")

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

    def _target_changed(self) -> None:
        """The Target page's own "Write to" changed.

        It only re-laid out the page. The size box is locked while a card is
        selected, because a card's capacity is not a matter of opinion - but
        switching to an image file here never asked again, so the box stayed
        locked at whatever a card had last put in it and a size that did not
        fit could not be corrected.
        """
        self._target_settled()

    def _card_it_will_not_fit(self, size: int):
        """A card this image is nearly the size of, but slightly too big for.

        Not any smaller card: someone building a 128 GB image with a 64 GB
        card in the reader has not made a mistake. One that overshoots by a
        few percent is a different thing - it was meant for that card - and
        that is the case that costs a write and an hour.
        """
        for card in self.device_list or []:
            if card.size and card.size < size <= card.size * 1.05:
                return card
        return None

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
        text = self.file_size_row.get_text()
        try:
            size = parse_size(text)
        except ValueError as error:
            self.quick_size_info.set_subtitle(str(error))
            return
        note = describe_size(size)
        card = size / 1000 ** 3
        if self.target_row.get_selected() == 1:
            note += f" - needs a card of at least {card:.0f} GB"
        wont_fit = self._card_it_will_not_fit(size)
        if wont_fit is not None:
            over = size - wont_fit.size
            note += (f" - WARNING: {human_size(over)} too big for "
                     f"{wont_fit.name}, which holds "
                     f"{describe_size(wont_fit.size)}. Set the size to "
                     f"{exact_size_text(wont_fit.size)} to fit it.")
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

    def _on_accelerator_changed(self) -> None:
        """Only an accelerator has a processor worth asking about.

        A stock machine's is whatever it shipped with, and a PiStorm's is
        whatever Emu68 provides - neither is a choice, so neither is offered.
        """
        chosen = list(machines.Accelerator)[
            self.quick_accelerator.get_selected()]
        self.quick_accelerator_cpu.set_visible(
            chosen is machines.Accelerator.ACCELERATOR)
        #  No PiStorm, no Raspberry Pi, and nothing that needs one.
        self.quick_pi.set_visible(chosen is machines.Accelerator.PISTORM)
        self._refresh_packages()
        self._refresh_boot_addons()
        self._on_layout_changed()
        #  Whether a disc's Kickstart is any use depends on what loads it.
        self._suit_the_rom_to_the_release()

    def _pi(self) -> machines.Pi:
        """The Raspberry Pi on the board, as the rows currently say."""
        choices = getattr(self, "_pi_choices", None) or self._machine().pi_models
        index = min(self.quick_pi.get_selected(), len(choices) - 1)
        return choices[max(index, 0)]

    def _refresh_pi_choices(self) -> None:
        """Offer the Pis this board takes, keeping the one already chosen.

        The board decides the list - a PiStorm16 is a Compute Module carrier
        and has no other option - so the list is rebuilt whenever the model
        changes.  A choice that survives the change is kept: changing the
        Amiga is not a statement about which Pi is plugged into it.
        """
        wanted = getattr(self, "_pi_choices", None)
        wanted = (wanted[self.quick_pi.get_selected()]
                  if wanted and self.quick_pi.get_selected() < len(wanted)
                  else None)
        self._pi_choices = list(self._machine().pi_models)
        was, self._ready = self._ready, False
        try:
            self.quick_pi.set_model(
                combo([pi.label for pi in self._pi_choices]))
            self.quick_pi.set_selected(
                self._pi_choices.index(wanted) if wanted in self._pi_choices
                else 0)
        finally:
            self._ready = was
        #  With no PiStorm there is no Pi to ask about.
        self.quick_pi.set_visible(
            self._accelerator() is machines.Accelerator.PISTORM)

    def _chip_ram(self) -> int:
        """The chip RAM the rows currently say this machine has, in KB."""
        options = (getattr(self, "_chip_ram_choices", None)
                   or list(self._machine().chip_ram_options))
        index = min(self.quick_chip_ram.get_selected(), len(options) - 1)
        return options[max(index, 0)]

    def _refresh_chip_ram_choices(self) -> None:
        """Offer what this model's Agnus can be, keeping the answer given.

        An answer that survives the change is kept - moving from an A500 to an
        A500+ is not a statement that the memory came out - and one the new
        model cannot have falls back to its stock figure.
        """
        wanted = getattr(self, "_chip_ram_choices", None)
        wanted = (wanted[self.quick_chip_ram.get_selected()]
                  if wanted and self.quick_chip_ram.get_selected() < len(wanted)
                  else None)
        self._chip_ram_choices = list(self._machine().chip_ram_options)
        was, self._ready = self._ready, False
        try:
            self.quick_chip_ram.set_model(
                combo([machines.chip_ram_label(k)
                       for k in self._chip_ram_choices]))
            self.quick_chip_ram.set_selected(
                self._chip_ram_choices.index(wanted)
                if wanted in self._chip_ram_choices else 0)
        finally:
            self._ready = was
        #  A machine with one possible size is not being asked anything.
        self.quick_chip_ram.set_visible(len(self._chip_ram_choices) > 1)

    def _on_chip_ram_changed(self) -> None:
        if not self._ready:
            return
        self._refresh_boot_addons()
        self._update_summary()

    def _on_pi_changed(self) -> None:
        if not self._ready:
            return
        #  Which software is on offer follows the Pi as well as the chipset
        #  and the screen, so the list has to be rebuilt - and the USB socket
        #  question only exists on a Pi that has more than one.
        self._refresh_packages()
        self._refresh_boot_addons()
        self._update_summary()

    def _accelerator(self) -> machines.Accelerator:
        return list(machines.Accelerator)[
            self.quick_accelerator.get_selected()]

    def _accelerator_cpu(self) -> machines.Cpu | None:
        if self._accelerator() is not machines.Accelerator.ACCELERATOR:
            return None
        return list(machines.Cpu)[self.quick_accelerator_cpu.get_selected()]

    def _machine_boot_values(self) -> dict:
        """The Emu68 settings the machine and the screen decide."""
        wanted = machines.boot_options(self._machine(), self._display())
        return {"slowdown": wanted.chip_slowdown, "vbr": wanted.vbr_move,
                "vc4": int(wanted.vc4_mem or 0)}

    def _derive_boot_rows(self) -> None:
        """Set the machine's Emu68 settings, unless they were set by hand.

        They were only ever set by the quick setup, so changing the machine
        or the screen on the pages left them as the previous machine had
        them: no chip RAM slowdown on an A500 chosen after an A1200. A row
        still holding what was last derived follows the new machine; one
        somebody changed keeps their value, and the build says so if it
        contradicts the machine.
        """
        rows = {"slowdown": (self.slowdown_row.get_active,
                             self.slowdown_row.set_active),
                "vbr": (self.vbr_row.get_active, self.vbr_row.set_active),
                "vc4": (lambda: int(self.vc4_row.get_value()),
                        self.vc4_row.set_value)}
        values = self._machine_boot_values()
        last = getattr(self, "_derived_boot", None)
        for key, (read, write) in rows.items():
            if last is None or read() == last[key]:
                write(values[key])
        self._derived_boot = values

    def _on_display_changed(self) -> None:
        #  Which software suits the card follows the screen it is watched on,
        #  and nothing rebuilt the list when that changed: choosing a display
        #  that draws on the Pi's HDMI left Picasso96 - the RTG subsystem the
        #  choice depends on - sitting there unticked.
        self._derive_boot_rows()
        self._refresh_packages()
        self._sync_visibility()
        self._on_layout_changed()

    def _on_machine_changed(self) -> None:
        if not self._ready:
            return
        self._derive_boot_rows()
        machine = self._machine()
        self.quick_machine_hint.set_subtitle(
            f"{machine.board_label} - {machine.chipset.value} chipset "
            f"({machine.chipset.native_colours})")
        #  Which content categories are worth copying follows the machine,
        #  and so does which software suits its chipset.
        self._refresh_categories()
        #  The board decides which Raspberry Pi is even possible, so the Pi
        #  list is rebuilt before the software list that depends on it.
        self._refresh_pi_choices()
        self._refresh_chip_ram_choices()
        self._refresh_packages()
        self._refresh_boot_addons()
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
        #  A split build lays its drives out on their own target, not on the
        #  Pi's boot card.
        if self._task is builder.Task.SPLIT:
            size = self._drives_target()[2]
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

    def _suggest_layout(self) -> None:
        """Redraw the drives from the machine, the system and the sizes.

        A layout somebody arranged, or one that came with a saved setup, is
        left alone as the choices before it change; this is how to have the
        suggestion back.
        """
        self._derived_partitions = None
        self._relayout_partitions()
        self._update_summary()

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
            config = self.gather(require_target=False)
        except Exception as error:               # noqa: BLE001
            self.quick_plan.set_text(str(error))
            return
        #  A split build is described as the two things it writes, before
        #  either has been given a place to go.
        if self._task is builder.Task.SPLIT and not config.drives_target:
            config = dataclasses.replace(config,
                                         drives_target="not chosen yet")
        self.quick_plan.set_text(presets.describe_machine_setup(
            config, self._machine(), self._display(), detected))

    def _missing_choices(self) -> list[str]:
        """What still has to be decided before writing makes sense.

        validate() covers what would make the build fail outright; this is the
        rest - the things without which a card would be written and then not
        boot.  A Kickstart it has no ROM for, an install from floppies with no
        floppies.
        """
        try:
            config = self.gather(require_target=False)
        except Exception as error:               # noqa: BLE001
            return [str(error).rstrip(".")]
        missing = [problem.rstrip(".") for problem in config.validate()]
        if not config.target and config.mode is not builder.BuildMode.EXPORT:
            missing.insert(0, "a card or an image file to write to, on the "
                              "Target step")
            #  Said once, in the words above.
            missing = [m for m in missing if m != "No target selected"]
        if self._task is builder.Task.SPLIT and not config.drives_target:
            missing.insert(0, "where the Amiga drives go, on the Target step")

        if config.mode is builder.BuildMode.EXPORT:
            #  Reading drives out of an image needs an image, a folder and a
            #  tick - all of which validate() covers. It needs no Kickstart,
            #  no Emu68 and no card, and asking for them left the page saying
            #  "Still needed: a Kickstart ROM" for a task that writes no card.
            return missing

        if config.mode is builder.BuildMode.IMAGE:
            #  A prepared system brings its own everything; the image and a
            #  card is the whole of it.
            return missing

        #  No Kickstart is not a gap: with none on the boot partition, Emu68
        #  uses the ROM chip in the Amiga, as a card set up by hand does.
        if config.install_emu68 and not config.emu68_archive \
                and not config.emu68_prepared_dir and not self.releases:
            missing.append("an Emu68 release - still looking, or choose a "
                           "local archive on the Source page")
        #  install_amigaos is only true once a folder has been chosen, so
        #  asking about it alone meant a card that needs the disks and has
        #  none said nothing at all - and built, unbootable. What decides it
        #  is what the setup needs, which is known before any folder is.
        #  A CD carries the whole operating system, so a build taking one
        #  wants no floppies at all - and asking for them anyway is what kept
        #  Apply switched off with a 3.9 disc chosen and nothing missing.
        if self._system_source() == "cd":
            if not config.os_cd:
                missing.append(f"an AmigaOS {amigacd.release_names()} CD "
                               f"image")
            elif not self._os_cd_usable():
                missing.append(f"a CD image this recognises as AmigaOS "
                               f"{amigacd.release_names()}")
            return missing

        needs_disks = (config.install_amigaos
                       or self._system_source() == "adf"
                       or self._imported_needs_floppies())
        if needs_disks:
            if not config.adf_folder:
                missing.append("a folder of Workbench floppy images - the "
                               "drive you are importing brings no Workbench "
                               "of its own"
                               if self._imported_needs_floppies() else
                               "a folder of Workbench floppy images")
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
        #  The task is chosen on the first screen and fixed for the journey;
        #  this row only holds it for the code that reads the mode.
        self.mode_group = group

        self.image_group = Adw.PreferencesGroup(
            title="Image",
            description="A whole card - CaffeineOS, an Emu68 Hatcher image, "
                        "any .img backup - is written as it is. An Amiga "
                        "drive - a WinUAE, FS-UAE or HstWB .hdf - gets an "
                        "Emu68 boot partition built around it. Which one a "
                        "file is, it says itself. Compressed images (.xz, "
                        ".gz, .zip, .7z) are streamed straight to the card, so "
                        "no scratch space is needed.")
        self.image_row = FileRow("Image file",
                                 filters=IMAGE_FILTERS + HDF_FILTERS,
                                 on_change=lambda _p: self._on_image_chosen())
        self.image_group.add(self.image_row)
        self.image_info = Adw.ActionRow(title="Image details", subtitle="No image selected")
        self.image_info.set_sensitive(False)
        self.image_group.add(self.image_info)
        page.add(self.image_group)

        page.add(self.group_primary)

        self.hdf_group = Adw.PreferencesGroup(
            title="The Amiga drive",
            description="The card's partition table and boot partition are "
                        "created around it.")
        #  Filled from the image chosen above when that is a drive; there is
        #  one place to choose an image, not two.
        self.hdf_row = FileRow("Hard disk image (.hdf)", filters=HDF_FILTERS,
                               on_change=lambda _p: self._on_hdf_chosen())
        self.hdf_row.set_visible(False)
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
        #  Some software names the oldest Emu68 it works with, so which build
        #  is chosen decides what is on offer - and the list arrives from
        #  GitHub after the window is up, so this fires then too.
        #  Add-ons and kernels name the oldest Emu68 they work with too, so
        #  they follow the release as well - they were left in whatever state
        #  the previous release gave them.
        self.release_row.connect("notify::selected",
                                 lambda *_a: (self._refresh_packages(),
                                              self._refresh_boot_addons(),
                                              self._on_kernel_changed()))
        group.add(self.release_row)
        #  A kernel published outside the official release. It replaces only
        #  the kernel: the firmware, the device tree, the overlays and
        #  config.txt all still come from the release chosen above, which is
        #  why this is a row beside that one rather than an entry in it.
        self.kernel_row = Adw.ComboRow(
            title="Emu68 kernel",
            model=combo(["The one in the release above"]
                        + [k.label for k in emu68.KERNELS]))
        self.kernel_row.connect("notify::selected",
                                lambda *_a: self._on_kernel_changed())
        group.add(self.kernel_row)
        self.kernel_hint = Adw.ActionRow(title="", subtitle="")
        self.kernel_hint.set_sensitive(False)
        group.add(self.kernel_hint)
        self.local_zip_row = FileRow(
            "Use a local Emu68 zip instead",
            "Leave empty to download the version chosen above",
            filters=ZIP_FILTERS)
        group.add(self.local_zip_row)
        #  On the Emu68 & boot page, with the rest of what goes on the boot
        #  partition; built here because the rows above it read it.
        self.emu68_group = group
        return page

    def _chosen_kernel(self) -> "emu68.Kernel | None":
        index = self.kernel_row.get_selected() - 1
        if 0 <= index < len(emu68.KERNELS):
            return emu68.KERNELS[index]
        return None

    def _on_kernel_changed(self) -> None:
        """Say what the chosen kernel is, and refuse one that cannot be used.

        A kernel from a fork is published for some boards and not others, and
        is built against an Emu68 too new for the older releases. Leaving it
        selected where it cannot be laid down would be a card built from the
        release's own kernel with the row still saying otherwise.
        """
        kernel = self._chosen_kernel()
        if kernel is None:
            self.kernel_hint.set_subtitle(
                "The official Emu68 kernel, which is what almost every card "
                "wants.")
            self._refresh_packages()
            return
        variant = emu68.VARIANTS[min(self.variant_row.get_selected(),
                                     len(emu68.VARIANTS) - 1)].key
        tag = self._release_tag()
        lines = [kernel.description]
        if not emu68.kernel_suits(kernel, variant, tag):
            if variant not in kernel.assets:
                lines.append("There is no build of it for this board, so the "
                             "release's own kernel will be used.")
            else:
                version = ".".join(str(part) for part in kernel.min_release)
                lines.append(f"It needs Emu68 {version} or newer above it, so "
                             f"the release's own kernel will be used.")
            self.kernel_row.set_selected(0)
            self.kernel_hint.set_subtitle("  ".join(lines))
            return
        lines.append("The rest of the boot partition still comes from the "
                     "release chosen above.")
        lines += list(kernel.notes)
        self.kernel_hint.set_subtitle("  ".join(lines))
        self._refresh_packages()

    def _page_amiga(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        self.page_amiga = page
        #  Which Amiga this is, and how it is being looked at, decides most of
        #  what follows on this page.
        page.add(self.group_hardware)

        #  This belongs with the display, not with the image chooser it used
        #  to sit under: a drive imported onto a card this build partitions
        #  carries a saved screen mode exactly as a whole prepared image
        #  does, and there was no way to ask for it to be dealt with.
        group = Adw.PreferencesGroup(
            title="A system that was built elsewhere",
            description="Anything ready-made - a prepared card image, or a "
                        "drive imported from an .hdf - was set up on somebody "
                        "else's machine, watched on somebody else's screen.")
        self.patch_display_row = Adw.SwitchRow(
            title="Adapt the display after writing",
            subtitle="It keeps its own drivers, which are right for it, but "
                     "not its saved screen mode. Where this card has no RTG "
                     "display, clear it so Workbench opens on the Amiga's own "
                     "screen instead of one that is not there.")
        self.patch_display_row.connect("notify::active",
                                       lambda *_a: self._update_summary())
        group.add(self.patch_display_row)
        self.display_group = group
        page.add(group)

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

        #  WHDLoad wants Commodore's ROMs under its own names beside its
        #  relocation tables; these can be a different set from the one
        #  Kickstart the card boots.
        self.whdload_rom_group = Adw.PreferencesGroup(
            title="Kickstarts for WHDLoad",
            description="Games that boot their own Kickstart need the ROM "
                        "it was written for. Every ROM in this folder that "
                        "WHDLoad can use is recognised by its contents, "
                        "decrypted, and copied to Devs/Kickstarts under the "
                        "name WHDLoad looks for.")
        self.whdload_rom_row = FileRow(
            "Folder of Kickstart ROMs",
            "The Kickstart ROM's own folder", folder=True,
            on_change=lambda _p: self._scan_whdload_roms())
        self.whdload_rom_group.add(self.whdload_rom_row)
        self.whdload_rom_info = Adw.ActionRow(title="Recognised",
                                              subtitle="Choose a folder")
        self.whdload_rom_info.set_sensitive(False)
        self.whdload_rom_group.add(self.whdload_rom_info)
        page.add(self.whdload_rom_group)

        self.os_group = Adw.PreferencesGroup(
            title="Workbench floppy images",
            description="Used when the operating system above is set to "
                        "\u201cinstall from my floppy images\u201d. "
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

        #  A release sold on CD is a source of its own rather than another
        #  release in the floppy list.
        group = Adw.PreferencesGroup(
            title=f"AmigaOS {amigacd.release_names()} from CD",
            description="These releases came on CD. Point at the disc "
                        "image and the whole system is installed from it, in "
                        "the order the disc's own installer uses. 3.5 and 3.9 "
                        "need a 68020 or better and a Kickstart 3.1 (V40) "
                        "ROM; 3.2 runs on a Kickstart 3.2 ROM, or on a 3.1 "
                        "ROM with the Kickstart modules from its disc.")
        self.os_cd_row = FileRow("AmigaOS CD image (.iso)",
                                 filters=[("CD images", ["*.iso", "*.ISO"])],
                                 on_change=lambda _p: self._on_os_cd_chosen())
        group.add(self.os_cd_row)
        self.os_cd_details = Adw.ActionRow(title="Disc", subtitle="No CD selected")
        self.os_cd_details.set_sensitive(False)
        group.add(self.os_cd_details)
        #  What a disc's installer asks is the disc's to say, so there is a
        #  switch for every question any release has and each is shown only
        #  for a disc that asks it.
        self.os_cd_options: dict[str, Adw.SwitchRow] = {}
        for release in amigacd.RELEASES:
            for option in release.options:
                if option.key in self.os_cd_options:
                    continue
                row = Adw.SwitchRow(title=self._as_markup(option.label),
                                    subtitle=self._as_markup(
                                        option.description))
                row.set_active(option.default)
                row.set_visible(False)
                row.connect("notify::active",
                            lambda *_a: self._update_summary())
                group.add(row)
                self.os_cd_options[option.key] = row
        self.boingbag_row = FileRow(
            "BoingBag archives folder", folder=True,
            subtitle="The update packs, as downloaded (.lha). Every pack that "
                     "belongs to the chosen release is applied, oldest first.",
            on_change=lambda _p: self._on_boingbags_chosen())
        group.add(self.boingbag_row)
        self.boingbag_found = Adw.ActionRow(title="Updates found",
                                            subtitle="No folder selected")
        self.boingbag_found.set_sensitive(False)
        group.add(self.boingbag_found)
        self.boingbag_community = Adw.SwitchRow(
            #  Escaped: these titles go through Pango markup, and a bare
            #  ampersand makes it refuse the whole string.
            title="Include BoingBags 3 &amp; 4",
            subtitle="A community release that supersedes much of BoingBags 1 "
                     "and 2 and adds LBA48 large-disk support. It changes core "
                     "components, so turn it off for a stock 3.9.")
        self.boingbag_community.set_active(True)
        self.boingbag_community.connect("notify::active",
                                        lambda *_a: self._update_summary())
        group.add(self.boingbag_community)
        self.boingbag_emulator = Adw.SwitchRow(
            title="Apply locked updates with FS-UAE",
            subtitle="BoingBags 1 and 2 for 3.9 keep their system fixes in an "
                     "encrypted archive only their own Updater can open. With "
                     "FS-UAE installed it is run here; without it those fixes "
                     "are listed as left out.")
        self.boingbag_emulator.set_active(True)
        self.boingbag_emulator.connect("notify::active",
                                       lambda *_a: self._update_summary())
        group.add(self.boingbag_emulator)
        self.os_cd_group = group
        page.add(group)

        return page


    def _page_packages(self) -> Adw.PreferencesPage:
        """The software to add, on a page of its own.

        It shared the Amiga page with the model, the Kickstart and the
        Workbench disks, which are facts about the hardware; this is a
        shopping list, and it is longer than everything else put together.
        """
        page = Adw.PreferencesPage()
        self.page_packages = page
        self.packages_group = Adw.PreferencesGroup(
            title="Software to add",
            description="Fetched from each publisher - Aminet, or the project "
                        "that makes it - and cached between builds.")
        suggest = Gtk.Button(label="Suggested load", valign=Gtk.Align.CENTER,
                             tooltip_text="Tick what suits this machine, "
                                          "chipset and display")
        suggest.add_css_class("flat")
        suggest.connect("clicked", lambda *_a: self._apply_suggested_packages())
        self.packages_group.set_header_suffix(suggest)
        #  A drive imported from an image usually has its own copy of some of
        #  this. The file system creates files and never overwrites them, so
        #  one of the two wins by landing first - which is not a decision the
        #  build should be making quietly on somebody's behalf.
        self.replace_older_row = Adw.SwitchRow(
            title="Replace older copies already on the imported drive",
            subtitle="A ready-made drive often carries its own WHDLoad, "
                     "icon.library and the like, sometimes years old. On, the "
                     "release you ticked is installed in its place; off, "
                     "whatever the drive already has is kept and the download "
                     "is left out.")
        self.replace_older_row.set_active(True)
        self.replace_older_row.connect("notify::active",
                                       lambda *_a: self._update_summary())
        self.packages_group.add(self.replace_older_row)
        #  What a fresh window starts with is the same recommendation the
        #  "suggest a set" button makes, for the machine and screen the
        #  window opens on.  Ticking ``package.default`` directly is what let
        #  the two drift apart: the flags said one set and the button said
        #  another.
        starting = set(packages.suggested(machines.MACHINES[0],
                                          list(machines.Display)[0]))
        self.package_rows: dict[str, PackageCheck] = {}
        self.package_groups: list[Adw.PreferencesGroup] = [self.packages_group]
        self.packages_group.add(self._software_browser(starting))
        page.add(self.packages_group)

        #  Pictures and text some software can bring, which it does not need
        #  to run: asked about, never assumed. One switch for each ticked
        #  package the catalogue says has some.
        self.media_group = Adw.PreferencesGroup(
            title="Pictures and extras",
            description="Not needed for the software to work, and sometimes "
                        "large - included only if you want them.")
        self.media_rows: dict[str, Adw.SwitchRow] = {}
        for package in packages.CATALOGUE:
            if not package.media:
                continue
            row = Adw.SwitchRow(
                title=GLib.markup_escape_text(f"{package.label}: include "
                                              f"{package.media}"))
            row.set_active(False)
            row.connect("notify::active", lambda *_a: self._update_summary())
            self.media_rows[package.key] = row
            self.media_group.add(row)
        self.media_group.set_visible(False)
        page.add(self.media_group)

        #  A prepared drive can carry its own copy of a chosen program under
        #  a different name entirely - ClassicWB keeps SysInfo 3.24 from 1993
        #  in Tools/SysInfo while the package installs 4.4 into
        #  Utilities/SysInfo - so both land and only one is ever opened.
        #  Removing somebody's software is not a thing to do quietly, so each
        #  one is listed and can be kept.
        self.older_group = Adw.PreferencesGroup(
            title="Older copies already on the drive",
            description="The drive you are building on carries its own copy "
                        "of some of what you ticked, under its own name. "
                        "These are removed so only the version you chose is "
                        "on the card. Turn one off to keep both.")
        self.older_rows: dict[str, Adw.SwitchRow] = {}
        self.older_group.set_visible(False)
        page.add(self.older_group)

        #  A ready-made distribution arrives with its own idea of what
        #  belongs on a card, and until now it was all of it or none.
        self.arrives_group = Adw.PreferencesGroup(
            title="Software the drive already has",
            description="What the image you are building on brings with it. "
                        "Turn one off to leave it out - the drawer, what is "
                        "in it and its icon.")
        self.arrives_rows: dict[str, Adw.SwitchRow] = {}
        self.arrives_group.set_visible(False)
        page.add(self.arrives_group)

        #  Software the drive brings that cannot work here at all - written
        #  for another machine, or asking for a device or a volume this card
        #  has not got. Shown separately from the rest, because "you may not
        #  want this" and "this cannot work" are different statements.
        self.broken_group = Adw.PreferencesGroup(
            title="Software that cannot work on this card",
            description="Each of these was checked against the card being "
                        "built and needs something it will not have. They are "
                        "removed unless you turn one back on.")
        self.broken_rows: dict[str, Adw.SwitchRow] = {}
        self.broken_group.set_visible(False)
        page.add(self.broken_group)

        #  Clutter: drawers that are empty, that only mean something inside an
        #  emulator, or assigns left pointing at something this build removes.
        #  Discovered from the drive rather than named anywhere, because a list
        #  of paths is right for one distribution and finds nothing on the next.
        #  Removing is destructive, so every one of these is offered and the
        #  uncertain ones default to keeping what is there.
        self.clutter_group = Adw.PreferencesGroup(
            title="Clutter this card has no use for",
            description="Found by looking at the drive, not from a list: "
                        "drawers holding nothing, scripts that only work "
                        "inside an emulator, and assigns pointing at what you "
                        "are leaving out. Turn one on to remove it.")
        self.clutter_rows: dict[str, Adw.SwitchRow] = {}
        #  What each row was last set to by this code, so an answer the user
        #  has given can be told apart from one that is still the default.
        self._clutter_default: dict[str, bool] = {}
        self.clutter_group.set_visible(False)
        page.add(self.clutter_group)

        #  Separate from the clutter list on purpose. "Take this off the
        #  desktop" and "take this off the card" are different requests, and
        #  answering the first with the second would delete somebody's
        #  program: an icon named in .backdrop is shown on the desktop
        #  *instead of* inside its drawer, so dropping the line puts it back
        #  where the file already is and removes nothing.
        self.desktop_group = Adw.PreferencesGroup(
            title="Icons on the Workbench desktop",
            description="What the drive puts on the desktop rather than in a "
                        "drawer. Turn one off to have it sit in its own drawer "
                        "instead - nothing is deleted either way.")
        self.desktop_rows: dict[str, Adw.SwitchRow] = {}
        self.desktop_group.set_visible(False)
        page.add(self.desktop_group)


        #  Which USB socket the Amiga is given.  The driver numbers its units
        #  by path rather than by socket - unit 0 is the Pi's onboard OTG port
        #  and the four USB-A sockets on a Pi 4B are unit 1 - so leaving it at
        #  the driver's own default would attach the stack to a socket nobody
        #  plugs anything into.  It appears only when something chosen needs
        #  the answer, which is found by looking at the catalogue.
        self.usb_group = Adw.PreferencesGroup(
            title="USB",
            description="Which of the Pi's USB paths the Amiga is given. "
                        "This is written into the startup line that attaches "
                        "the stack, and the OTG port also needs a config.txt "
                        "line to become a host port at all - both are set for "
                        "you from this.")
        self.usb_port_row = Adw.ComboRow(title="Plug USB devices into",
                                         model=combo(["No USB stack chosen"]))
        self.usb_port_row.connect("notify::selected",
                                  lambda *_a: self._update_summary())
        self.usb_group.add(self.usb_port_row)
        self.usb_group.set_visible(False)
        self.package_groups.append(self.usb_group)
        page.add(self.usb_group)

        #  The defaults are set row by row above, which never goes through the
        #  toggle, so what they need has to be ticked once they all exist.
        self._tick_what_is_needed()
        self._count_software()
        return page

    #  How tall the software browser is. Fixed, so the page around it stays
    #  short: the list scrolls inside it rather than the page growing to the
    #  length of the catalogue.
    SOFTWARE_HEIGHT = 380
    #  The two views that are not a category: what is ticked, and everything.
    CHOSEN, EVERYTHING = "chosen", "all"

    def _software_browser(self, starting: set[str]) -> Gtk.Widget:
        """The catalogue: categories on the left, their software on the right.

        One category at a time, a line each, with the reason first where a
        package is held on or cannot be had. A search box finds a package in
        any of them, and *Chosen* lists what the card will carry. It took the
        place of a page of switch rows several screens long, and then of a
        window wide enough to show everything at once - which was too big.
        """
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.add_css_class("card")
        self.software_search = Gtk.SearchEntry(
            placeholder_text="Find software", margin_top=8, margin_bottom=8,
            margin_start=8, margin_end=8)
        self.software_search.connect("search-changed",
                                     lambda *_a: self._refilter_software())
        frame.append(self.software_search)
        frame.append(Gtk.Separator())
        panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                        height_request=self.SOFTWARE_HEIGHT)
        frame.append(panes)

        #  The views, each with how much of it is ticked.
        self.software_sidebar = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.SINGLE)
        self.software_sidebar.add_css_class("navigation-sidebar")
        self.software_counts: dict[object, Gtk.Label] = {}
        views = [(self.CHOSEN, "Chosen"), (self.EVERYTHING, "Everything")] \
            + [(c, c.value) for c in packages.Category
               if packages.in_category(c)]
        self._software_views = [view for view, _title in views]
        for view, title in views:
            line = Gtk.Box(spacing=8, margin_start=4, margin_end=4)
            name = Gtk.Label(label=title, xalign=0, hexpand=True,
                             ellipsize=Pango.EllipsizeMode.END)
            count = Gtk.Label()
            count.add_css_class("dim-label")
            count.add_css_class("numeric")
            line.append(name)
            line.append(count)
            self.software_counts[view] = count
            self.software_sidebar.append(line)
        self.software_sidebar.connect(
            "row-selected", lambda *_a: self._refilter_software())
        side = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                  width_request=210)
        side.set_child(self.software_sidebar)
        panes.append(side)
        panes.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.software_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        for category in packages.Category:
            for package in packages.in_category(category):
                row = PackageCheck(package.label, package.description)
                row.set_active(package.key in starting)
                row.connect("notify::active",
                            lambda *_a, key=package.key:
                            self._on_package_toggled(key))
                row.connect("notify::active",
                            lambda *_a: self._count_software())
                for controller in (Gtk.EventControllerMotion(),
                                   Gtk.EventControllerFocus()):
                    controller.connect("enter", lambda *_a, key=package.key:
                                       self._describe_software(key))
                    row.add_controller(controller)
                row.set_margin_top(3)
                row.set_margin_bottom(3)
                row.set_margin_start(6)
                row.set_margin_end(6)
                holder = Gtk.ListBoxRow(child=row, activatable=False)
                holder.package = package
                self.package_rows[package.key] = row
                self.software_list.append(holder)
        self.software_list.set_filter_func(self._software_shown)
        self.software_list.set_header_func(self._software_header)
        self.software_empty = Gtk.Label(
            label="Nothing here", vexpand=True, valign=Gtk.Align.CENTER)
        self.software_empty.add_css_class("dim-label")
        self.software_list.set_placeholder(self.software_empty)
        listing = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     hexpand=True)
        listing.set_child(self.software_list)
        panes.append(listing)

        frame.append(Gtk.Separator())
        self.software_details = Gtk.Label(
            xalign=0, yalign=0, wrap=True, use_markup=True, lines=4,
            ellipsize=Pango.EllipsizeMode.END, margin_top=8,
            margin_bottom=10, margin_start=12, margin_end=12,
            height_request=72,
            label="Point at a package to see what it is and where it comes "
                  "from. Ticking one ticks what it needs as well.")
        frame.append(self.software_details)
        #  Opens on the first category rather than on an empty Chosen list
        #  for a card nothing has been decided for yet.
        self.software_sidebar.select_row(
            self.software_sidebar.get_row_at_index(2))
        return frame

    def _software_view(self):
        """The view chosen in the sidebar: CHOSEN, EVERYTHING or a category."""
        row = self.software_sidebar.get_selected_row()
        index = row.get_index() if row is not None else 0
        return self._software_views[index]

    def _software_shown(self, holder) -> bool:
        package = holder.package
        wanted = self.software_search.get_text().strip().lower()
        if wanted:
            return any(wanted in text.lower() for text in
                       (package.label, package.description, package.key,
                        package.category.value))
        view = self._software_view()
        if view == self.CHOSEN:
            return self.package_rows[package.key].get_active()
        return view == self.EVERYTHING or package.category is view

    def _software_header(self, holder, before) -> None:
        """Name the category where a list spans several of them."""
        spans = (self.software_search.get_text().strip()
                 or self._software_view() in (self.CHOSEN, self.EVERYTHING))
        if spans and (before is None
                      or before.package.category is not holder.package.category):
            label = Gtk.Label(label=holder.package.category.value, xalign=0,
                              margin_top=8, margin_start=12, margin_bottom=2)
            label.add_css_class("heading")
            holder.set_header(label)
        else:
            holder.set_header(None)

    def _refilter_software(self) -> None:
        if not hasattr(self, "software_list"):
            return
        searching = bool(self.software_search.get_text().strip())
        self.software_empty.set_label(
            "No software matches that" if searching
            else "Nothing ticked yet" if self._software_view() == self.CHOSEN
            else "Nothing here")
        self.software_list.invalidate_filter()
        self.software_list.invalidate_headers()

    def _describe_software(self, key: str) -> None:
        """Say what a package is, and what it is tied to, in the details strip."""
        package = packages.CATALOGUE_BY_KEY[key]
        row = self.package_rows[key]
        text = (f"<b>{GLib.markup_escape_text(package.label)}</b>  "
                f"<span alpha='70%'>{GLib.markup_escape_text(package.category.value)}"
                f"</span>\n{row.get_subtitle()}")
        #  Ties both ways, from the catalogue: what this one brings with it,
        #  and what that is switched on cannot do without it.
        needs = [packages.CATALOGUE_BY_KEY[k].label
                 for k in packages.expand([key]) if k != key]
        needed_by = [packages.CATALOGUE_BY_KEY[k].label
                     for k, other in self.package_rows.items()
                     if k != key and other.get_active()
                     and key in packages.expand([k])]
        ties = []
        if needs:
            ties.append("Brings with it: " + ", ".join(needs) + ".")
        if needed_by:
            ties.append("Needed by: " + ", ".join(needed_by) + ".")
        if ties:
            text += "\n<i>" + GLib.markup_escape_text("  ".join(ties)) + "</i>"
        self.software_details.set_markup(text)

    def _count_software(self) -> None:
        """Keep the counts in the sidebar in step with the ticks."""
        if not getattr(self, "software_counts", None):
            return
        #  The media question is only asked of what is being installed.
        if hasattr(self, "media_rows"):
            shown = False
            for key, row in self.media_rows.items():
                ticked = self.package_rows[key].get_active()
                row.set_visible(ticked)
                shown |= ticked
            self.media_group.set_visible(shown)
        ticked = {key for key, row in self.package_rows.items()
                  if row.get_active()}
        for view, label in self.software_counts.items():
            if view == self.CHOSEN:
                label.set_label(str(len(ticked)))
            elif view == self.EVERYTHING:
                label.set_label(str(len(self.package_rows)))
            else:
                members = [p.key for p in packages.in_category(view)]
                label.set_label(f"{sum(k in ticked for k in members)}"
                                f"/{len(members)}")
        #  What is ticked has changed, so the Chosen list has too.
        if self._software_view() == self.CHOSEN \
                and not self.software_search.get_text().strip():
            self._refilter_software()

    def _page_storage(self) -> Adw.PreferencesPage:
        """How the card is divided up.

        Kept apart from the Amiga page deliberately: how big the drives are and
        what file system they carry is a different question from what gets
        written into them, and mixing the two made a long page where neither
        was easy to find.
        """
        page = Adw.PreferencesPage()

        page.add(self.group_sizes)

        #  Some machines keep their storage elsewhere - a second card in a CF
        #  adapter, a disk on the IDE port - and want the card to be Emu68 and
        #  a Kickstart and nothing more. An empty partition is not the same
        #  answer: it still claims the rest of the card and still comes up
        #  asking to be initialised.
        self.boot_only_group = Adw.PreferencesGroup(
            title="What this card carries",
            description="Emu68 boots the machine from the FAT32 partition. The "
                        "Amiga's drives are a separate question.")
        self.boot_only_row = Adw.SwitchRow(
            title="Emu68 only, no Amiga drive",
            subtitle="For a machine whose storage is elsewhere. The rest of the "
                     "card is left unclaimed rather than formatted, so nothing "
                     "asks to be initialised.")
        self.boot_only_row.set_active(False)
        self.boot_only_row.connect("notify::active",
                                   lambda *_a: self._boot_only_changed())
        self.boot_only_group.add(self.boot_only_row)
        #  The mirror of it: drives and no boot partition.  A real accelerator
        #  with an IDE or SCSI controller reads a Rigid Disk Block at block 0
        #  and knows nothing about an MBR, so a card for one carries no
        #  partition table at all - the RDB is the partition table.
        self.amiga_only_row = Adw.SwitchRow(
            title="Amiga drives only, no Emu68 boot partition",
            subtitle="For a drive on the Amiga's own IDE or SCSI port - "
                     "behind a PiStorm that boots from its own card, or a "
                     "real accelerator. The Rigid Disk Block starts at block "
                     "0, where the controller looks for it, and there is no "
                     "FAT32 partition and no Emu68.")
        self.amiga_only_row.set_active(False)
        self.amiga_only_row.connect("notify::active",
                                    lambda *_a: self._amiga_only_changed())
        self.boot_only_group.add(self.amiga_only_row)
        page.add(self.boot_only_group)

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
        self.page_options = page
        page.add(self.emu68_group)

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

        #  Add-ons that go onto the boot partition beside the Emu68 kernel
        #  rather than onto an Amiga drive.  On the Options page, with the
        #  rest of what the boot partition carries, and not on the Packages
        #  page, which is about the software the Amiga runs.
        self.addon_group = Adw.PreferencesGroup(
            title="Boot partition add-ons",
            description="Installed beside the Emu68 kernel. Each is finished "
                        "on the Amiga by its own installer; what happens here "
                        "is the step whose instructions ask for a Windows PC. "
                        "Nothing here is downloaded - put the archive where "
                        "you keep your other Amiga material and it is found.")
        self.addon_rows: dict[str, Adw.SwitchRow] = {}
        for addon in bootaddon.CATALOGUE:
            row = Adw.SwitchRow(title=addon.label,
                                subtitle=addon.description)
            row.connect("notify::active",
                        lambda *_a, key=addon.key: self._on_addon_toggled(key))
            self.addon_rows[addon.key] = row
            self.addon_group.add(row)
        page.add(self.addon_group)

        group = Adw.PreferencesGroup(
            title="Emu68 options",
            description="Written to cmdline.txt, or to config.txt as device "
                        "tree overlays on Emu68 1.1 and later, which is where "
                        "several of these settings moved. Which form is used "
                        "follows the release being installed. See the Emu68 "
                        "documentation for the full list.")
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
        #  Kept, because an add-on can hold this switch on and has to be able
        #  to give the row its own words back afterwards.
        self._unit0_subtitle = ("Exposes the partition table and boot "
                                "partition read/write")
        self.unit0_row = Adw.SwitchRow(
            title="Allow the Amiga to write to the whole SD card",
            subtitle=self._unit0_subtitle)
        group.add(self.unit0_row)
        self.extra_row = Adw.EntryRow(title="Additional cmdline.txt options")
        group.add(self.extra_row)
        page.add(group)

        #  Settings Emu68 only gained in 1.1, and only as device tree
        #  overlays: there is no older spelling for them, so on an older
        #  release they cannot be written at all. The rows say so and are held
        #  off rather than being left looking as though they took.
        self.overlay_group = Adw.PreferencesGroup(
            title="Emu68 1.1 options",
            description="Settings that arrived with Emu68 1.1 and exist only "
                        "as device tree overlays.")
        self.noide_row = Adw.SwitchRow(
            title="Skip the check for an IDE hard disk",
            subtitle="On a machine with no drive on its IDE port, AmigaOS "
                     "spends a long time at every boot looking for one")
        self.overlay_group.add(self.noide_row)
        self.video_row = Adw.ComboRow(
            title="Video standard",
            subtitle="What Emu68 tells AmigaOS the machine is, whatever its "
                     "own Agnus says",
            model=combo(["As the Amiga reports it", "PAL", "NTSC"]))
        self.overlay_group.add(self.video_row)
        self.jit_row = Adw.SpinRow.new_with_range(0, 256, 1)
        self.jit_row.set_title("JIT cache (MB)")
        self.jit_row.set_subtitle("0 leaves the Emu68 default")
        self.jit_row.set_value(0)
        self.overlay_group.add(self.jit_row)
        page.add(self.overlay_group)

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
        #  "Amiga hard disk image (.hdf)" used to be a third choice here. It
        #  wrote the build's output as one bare drive, and a PiStorm card
        #  normally carries four - so it could not say which drive it was, and
        #  was wrong for every card this tool builds. Reading drives back out
        #  is its own task now: see "Export drives as .hdf".
        self.target_row = Adw.ComboRow(
            title="Write to",
            model=combo(["SD card", "SD card image file"]))
        self.target_row.connect("notify::selected",
                                lambda *_a: self._target_changed())
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
                                lambda *_a: (self._target_settled(),
                                             self._refresh_rewrite_drives(),
                                             self._update_summary()))
        self.device_group.add(self.device_row)
        page.add(self.device_group)

        self.file_group = Adw.PreferencesGroup(
            title="Image file",
            description="A sparse .img file you can write to a card later, or use "
                        "with an emulator.")
        self.file_row = SaveRow("Save image as", filters=IMAGE_FILTERS,
                                on_change=lambda _p: (
                                    self._refresh_rewrite_drives(),
                                    self._update_summary()))
        self.file_group.add(self.file_row)
        self.file_size_row = Adw.EntryRow(
            title="Image size - 32GB as cards are sold, 32GiB binary")
        self.file_size_row.set_text("32GB")
        #  The size shapes the layout, so the drives follow it.
        self.file_size_row.connect("changed", lambda _r: self._target_settled())
        self.file_group.add(self.file_size_row)
        self.quick_size_info = Adw.ActionRow(title="Size", subtitle="")
        self.quick_size_info.set_sensitive(False)
        self.file_group.add(self.quick_size_info)
        page.add(self.file_group)

        #  Rebuilding one drive: which one, read off the card itself.
        self.rewrite_group = Adw.PreferencesGroup(
            title="Drive to rebuild",
            description="Only this drive is written. It is formatted and "
                        "filled again with what the other pages choose; the "
                        "partition table, the boot partition and every other "
                        "drive are left exactly as they are. Anything on it "
                        "that the new build does not bring is lost, so back "
                        "it up first if it matters.")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             valign=Gtk.Align.CENTER,
                             tooltip_text="Read the card's drives again")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda _b: self._refresh_rewrite_drives())
        self.rewrite_group.set_header_suffix(refresh)
        self.rewrite_drive_row = Adw.ComboRow(
            title="Drive", model=combo(["Choose the card or image first"]))
        self.rewrite_drive_row.connect("notify::selected",
                                       lambda *_a: self._on_rewrite_drive())
        self.rewrite_group.add(self.rewrite_drive_row)
        #  For a card this account cannot read: the build runs with the
        #  rights to, and finds the drive by name there.
        self.rewrite_name_row = Adw.EntryRow(
            title="Drive name, as AmigaDOS mounts it - DH0, DH1...")
        self.rewrite_name_row.connect("changed",
                                      lambda _r: self._update_summary())
        self.rewrite_name_row.set_visible(False)
        self.rewrite_group.add(self.rewrite_name_row)
        self.rewrite_folder_row = FileRow(
            "Fill it from a folder",
            "Nothing - just the system and software chosen", folder=True,
            on_change=lambda _p: self._update_summary())
        self.rewrite_group.add(self.rewrite_folder_row)
        backup = Adw.ActionRow(
            title="Back this drive up first",
            subtitle="Takes you to Export with this card chosen, to save the "
                     "drive as an .hdf before it is rebuilt.")
        button = Gtk.Button(label="Export", valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda _b: self._back_up_rewrite_drive())
        backup.add_suffix(button)
        backup.set_activatable_widget(button)
        self.rewrite_group.add(backup)
        self._rewrite_drives: list[builder.Drive] = []
        self.rewrite_group.set_visible(False)
        page.add(self.rewrite_group)

        #  A PiStorm whose drives are elsewhere: where those go. The groups
        #  above are the Pi's boot card.
        self.drives_group = Adw.PreferencesGroup(
            title="Where the Amiga drives go",
            description="Workbench, the software and the rest of the drives, "
                        "with no boot partition - what the Amiga's IDE port "
                        "reads. A card is written directly; an image file "
                        "can be written to one later.")
        self.drives_kind_row = Adw.ComboRow(
            title="Write the drives to",
            model=combo(["Another card - a CF card or disk in a reader",
                         "An image file"]))
        self.drives_kind_row.set_selected(1)
        self.drives_kind_row.connect("notify::selected",
                                     lambda *_a: self._drives_target_changed())
        self.drives_group.add(self.drives_kind_row)
        self.drives_device_row = Adw.ComboRow(title="Card",
                                              model=combo([SELECT_CARD]))
        self.drives_device_row.connect(
            "notify::selected", lambda *_a: self._drives_target_changed())
        self.drives_group.add(self.drives_device_row)
        self.drives_file_row = SaveRow(
            "Save the drives as", filters=HDF_FILTERS,
            on_change=lambda _p: self._drives_target_changed())
        self.drives_group.add(self.drives_file_row)
        self.drives_size_row = Adw.EntryRow(
            title="Drive size - 32GB as cards are sold, 32GiB binary")
        self.drives_size_row.set_text("32GB")
        self.drives_size_row.connect("changed",
                                     lambda _r: self._drives_target_changed())
        self.drives_group.add(self.drives_size_row)
        self.drives_group.set_visible(False)
        page.add(self.drives_group)

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
        header = Adw.HeaderBar()
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
        #  Its own name: this was ``back_button`` too, built after the main
        #  window's, and from then on everything that showed or hid the
        #  window's Back was showing and hiding this one instead.
        self.progress_back_button = Gtk.Button(label="Back")
        self.progress_back_button.set_visible(False)
        self.progress_back_button.connect(
            "clicked", lambda _b: self.progress_window.close())
        buttons.append(self.progress_back_button)
        self.save_log_button = Gtk.Button(label="Save log…")
        self.save_log_button.set_visible(False)
        self.save_log_button.connect("clicked", self._on_save_log)
        buttons.append(self.save_log_button)
        box.append(buttons)

        view.set_content(box)
        return view

    # ------------------------------------------------------------- helpers

    def _choose_rewrite(self) -> None:
        self._start_task(builder.Task.REBUILD)

    def _choose_export(self) -> None:
        self._start_task(builder.Task.EXPORT)

    def _page_export(self) -> Adw.PreferencesPage:
        """Read the Amiga drives back out of a card, one .hdf each.

        The old answer wrote the build's *output* as a single bare drive,
        which cannot describe the four a PiStorm card carries. Here the drives
        are read from the image and offered by name, and each chosen one is
        written self-contained - its own Rigid Disk Block, and the handler the
        card embedded, so a PFS3 drive mounts with nothing else supplied.
        """
        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(
            title="Export drives as .hdf",
            description="Point at a card image, a backup or an .hdf, and lift "
                        "whichever drives you want out of it.")
        self.export_source = FileRow(
            "Image to read", "A card image, a backup, or an .hdf",
            filters=IMAGE_FILTERS + HDF_FILTERS,
            on_change=lambda _p: self._refresh_export_drives())
        group.add(self.export_source)
        self.export_dir = FileRow(
            "Export into", "Folder for the .hdf files", folder=True,
            on_change=lambda _p: self._update_summary())
        group.add(self.export_dir)
        page.add(group)

        #  One row per drive found, ticked to export. Nothing is listed until
        #  an image is chosen, because a guess about what a card holds is
        #  exactly what this feature exists to replace.
        self.export_group = Adw.PreferencesGroup(
            title="Drives in this image",
            description="Choose an image to see what it holds.")
        page.add(self.export_group)
        self.export_rows: dict[str, Adw.SwitchRow] = {}
        return page

    def _refresh_export_drives(self) -> None:
        """List what the chosen image actually holds."""
        if not hasattr(self, "export_group"):
            return
        for row in list(self.export_rows.values()):
            self.export_group.remove(row)
        self.export_rows.clear()
        path = self.export_source.path
        found = []
        if path:
            try:
                from ..core import export                    # noqa: PLC0415
                found = export.drives(path)
            except Exception as error:                       # noqa: BLE001
                self.export_group.set_description(f"Could not read it: {error}")
                self._update_summary()
                return
        if not path:
            self.export_group.set_description("Choose an image to see what it holds.")
        elif not found:
            self.export_group.set_description(
                "No Amiga drives found in this image - it has no Rigid Disk "
                "Block, so there is nothing to take out of it.")
        else:
            self.export_group.set_description(
                f"{len(found)} drive(s). Each ticked one becomes a separate "
                f"self-contained .hdf.")
        for drive in found:
            row = Adw.SwitchRow(title=drive.label, subtitle=
                                f"{drive.description}  ->  {drive.filename()}")
            #  Off to begin with. Every other list on this page defaults to
            #  what is already there, but this one writes new files, and a
            #  games drive is twenty gigabytes - so exporting all four
            #  because nobody said otherwise is not a sensible default. The
            #  Export button stays disabled until something is chosen.
            row.set_active(False)
            row.connect("notify::active", lambda *_a: self._update_summary())
            self.export_rows[drive.name] = row
            self.export_group.add(row)
        self._update_summary()

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

    def _rewrite_target(self) -> str:
        """The card or image the drive to rebuild is on, or "" if none yet."""
        if self._writing_to_device():
            card = self._selected_device()
            return card.path if card is not None else ""
        return self.file_row.path

    def _refresh_rewrite_drives(self) -> None:
        """List the drives on the chosen card, keeping the one already chosen.

        Read from the card's own Rigid Disk Block. Where it cannot be read -
        no permission to open the card - the name is asked for instead, and
        the build, which runs with the rights to, finds it by that name.
        """
        if not getattr(self, "_ready", False) \
                or self._mode() is not builder.BuildMode.REWRITE:
            return
        path = self._rewrite_target()
        wanted = self._rewrite_drive_name()
        drives = [d for d in builder.list_drives(path) if d.name] if path else []
        self._rewrite_drives = drives
        was, self._ready = self._ready, False
        try:
            if drives:
                self.rewrite_drive_row.set_model(
                    combo([d.label for d in drives]))
                names = [d.name.upper() for d in drives]
                #  The drive asked for before, or else the one that boots -
                #  which is the one most worth rebuilding.
                index = (names.index(wanted.upper()) if wanted.upper() in names
                         else next((i for i, d in enumerate(drives)
                                    if d.bootable), 0))
                self.rewrite_drive_row.set_selected(index)
                self.rewrite_drive_row.set_subtitle("")
            else:
                self.rewrite_drive_row.set_model(combo(
                    ["Choose the card or image first" if not path
                     else "No Amiga drives could be read"]))
                self.rewrite_drive_row.set_subtitle(
                    "" if not path else
                    "Type the drive's name below; the card is read again when "
                    "it is written.")
        finally:
            self._ready = was
        self.rewrite_name_row.set_visible(bool(path) and not drives)
        self._on_rewrite_drive()

    def _rewrite_drive(self) -> "builder.Drive | None":
        drives = getattr(self, "_rewrite_drives", [])
        index = self.rewrite_drive_row.get_selected()
        return drives[index] if 0 <= index < len(drives) else None

    def _rewrite_drive_name(self) -> str:
        drive = self._rewrite_drive()
        if drive is not None:
            return drive.name
        return self.rewrite_name_row.get_text().strip().upper()

    def _rewrite_boots(self) -> bool:
        """Whether the drive being rebuilt is the one the Amiga boots from.

        Unknown when the card could not be read; then the system is offered,
        and the build refuses it if the card says otherwise.
        """
        drive = self._rewrite_drive()
        return drive.bootable if drive is not None else True

    def _on_rewrite_drive(self) -> None:
        if not getattr(self, "_ready", False):
            return
        #  Only the drive that boots takes a Workbench and software; for any
        #  other the system pages have nothing to say.
        boots = self._rewrite_boots()
        if self._mode() is builder.BuildMode.REWRITE:
            self.os_group.set_visible(boots)
            for group in self.package_groups:
                group.set_visible(boots)
        self._update_summary()

    def _rewrite_spec(self) -> list[builder.AmigaPartitionSpec]:
        """The one drive being rebuilt, as the build is to fill it.

        Its size, file system and whether it boots are the card's, and the
        build reads them again from there; what is said here is only what to
        put on it, and the name it keeps on Workbench.
        """
        name = self._rewrite_drive_name()
        if not name:
            return []
        drive = self._rewrite_drive()
        volume = (drive.volume if drive is not None and drive.volume
                  else self.volume_row.get_text().strip() or name)
        return [builder.AmigaPartitionSpec(
            name, drive.size if drive is not None else None,
            drive.filesystem if drive is not None else "PFS3",
            self._rewrite_boots(), 0,
            content_folder=self.rewrite_folder_row.path,
            volume_name=volume)]

    def _back_up_rewrite_drive(self) -> None:
        """Export, with this card chosen, so the drive can be saved first."""
        path = self._rewrite_target()
        name = self._rewrite_drive_name()
        self._choose_export()
        if path:
            self.export_source.set_path(path)
        self._toast(f"Tick {name or 'the drive'} and choose a folder to save "
                    f"it in; then come back to rebuild it")

    def _drives_card(self):
        """The card chosen for a split build's drives, or None."""
        if self.drives_kind_row.get_selected() != 0:
            return None
        index = self.drives_device_row.get_selected() - 1
        if not self.device_list or index < 0 or index >= len(self.device_list):
            return None
        return self.device_list[index]

    def _drives_target(self) -> tuple[str, bool, int]:
        """Where a split build's drives go: the path, is it a card, the size."""
        card = self._drives_card()
        if card is not None:
            return card.path, True, card.size
        try:
            size = parse_size(self.drives_size_row.get_text())
        except ValueError:
            size = 8 * GIB
        if self.drives_kind_row.get_selected() == 0:
            return "", True, size
        return self.drives_file_row.path, False, size

    def _drives_target_changed(self) -> None:
        #  Writing the card's size into the size box changes the box, and
        #  the box's change comes back here: choosing a card for the drives
        #  recursed until Python gave up, and the window hung.
        if not self._ready or getattr(self, "_drives_changing", False):
            return
        self._drives_changing = True
        try:
            on_card = self.drives_kind_row.get_selected() == 0
            self.drives_device_row.set_visible(on_card)
            self.drives_file_row.set_visible(not on_card)
            card = self._drives_card()
            #  A card's size is the card's, as on the boot card's own target.
            self.drives_size_row.set_visible(not on_card)
            if card is not None:
                size = exact_size_text(card.size)
                if self.drives_size_row.get_text() != size:
                    self.drives_size_row.set_text(size)
            self._relayout_partitions()
            self._update_summary()
        finally:
            self._drives_changing = False

    def _making_hdf(self) -> bool:
        """Kept as False: the build no longer writes a bare Amiga drive.

        One file cannot describe the four drives a PiStorm card carries, so
        that option became "Export drives as .hdf", which writes one
        self-contained file per drive.
        """
        return False

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
        self.image_group.set_visible(mode in (builder.BuildMode.IMAGE,
                                              builder.BuildMode.HDF))
        self.hdf_group.set_visible(mode is builder.BuildMode.HDF)
        rebuilding = mode is builder.BuildMode.REWRITE
        self.partition_group.set_visible(mode is builder.BuildMode.FRESH)
        self.os_group.set_visible(mode is builder.BuildMode.FRESH
                                  or (rebuilding and self._rewrite_boots()))
        #  Anything this build lays out can have software added to it, not
        #  only a Workbench installed from floppies: an imported drive gets
        #  the same package overlays, and hiding the list meant a card built
        #  around somebody's drive could not be given WHDLoad or iGame.
        show_packages = (mode is builder.BuildMode.FRESH
                         or (rebuilding and self._rewrite_boots()))
        for group in self.package_groups:
            group.set_visible(show_packages)
        #  The floppies are offered alongside an imported drive too: a drive
        #  can boot and still bring no Workbench of its own - ClassicWB's
        #  asks for the disks on its first boot - and there was no way to
        #  say where they are.
        installing = (self._system_source() == "adf"
                      or (self.quick_hdf.path and self._imported_needs_floppies()))
        #  A drive that brings no Workbench needs the disks, and the chooser
        #  for them lives on the Source page - which a quick screen does not
        #  show. So it is brought to where the drive was chosen, beside it,
        #  or there is simply no way to say where the disks are.
        #  Only in the full workflow: a quick screen has already borrowed
        #  these into the group it shows, and moving them onto the Source
        #  page - which no quick screen shows - would take the chooser away
        #  from the very person who has to answer it.
        if self._imported_needs_floppies() and getattr(self, "_customising", True):
            for row in (self.adf_row, self.os_version_row, self.os_disks):
                self._move_row(row, self.group_primary)
        elif getattr(self, "_customising", True):
            for row in (self.adf_row, self.os_version_row, self.os_disks):
                self._move_row(row, self.os_group)
        for row in (self.adf_row, self.os_version_row, self.volume_row, self.os_disks):
            row.set_visible(installing)
        #  There is a saved screen mode to deal with only where a ready-made
        #  system is involved: a whole card image, a drive written unchanged,
        #  or a drive imported onto a card this build partitions. A Workbench
        #  installed from floppies has never been watched on anything.
        self.display_group.set_visible(
            mode not in builder.FILLS_DRIVES
            or any(row.spec().content_hdf for row in self.partition_rows)
            or bool(self.quick_hdf.path))
        #  Only a card that imports a drive can have the clash this settles.
        self.replace_older_row.set_visible(
            show_packages
            and (any(row.spec().content_hdf for row in self.partition_rows)
                 or bool(self.quick_hdf.path)))
        self.expand_group.set_visible(mode not in builder.FILLS_DRIVES)
        for row in self.extra_rows:
            row.set_visible(self.expand_row.get_active())
        #  Rebuilding a drive writes nothing to the boot partition, so there
        #  is no Emu68 to choose.
        install = (self.install_emu_row.get_active() and not making_hdf
                   and not rebuilding)
        self.install_emu_row.set_visible(not making_hdf and not rebuilding)
        for row in (self.variant_row, self.variant_hint, self.release_row,
                    self.local_zip_row):
            row.set_visible(install)

        #  Emu68 is what the rest of the card hangs off, and it is answered on
        #  the Source page - a page *before* the Storage one that used to rule
        #  it. So the decision runs this way round: Emu68 decides which of the
        #  two storage shapes can be asked for, not the other way about.
        #
        #    Emu68 on   - "Emu68 only, no Amiga drive" makes sense;
        #                 "Amiga drives only, no Emu68" contradicts it.
        #    Emu68 off  - the reverse.
        #
        #  A switch that contradicts one already made is turned off as well as
        #  disabled, so nothing is carried into the build that the page is no
        #  longer offering.
        wants_emu68 = self.install_emu_row.get_active()
        if not wants_emu68 and self.boot_only_row.get_active():
            self.boot_only_row.set_active(False)
        if wants_emu68 and self.amiga_only_row.get_active():
            self.amiga_only_row.set_active(False)
        self.boot_only_row.set_sensitive(wants_emu68)
        self.amiga_only_row.set_sensitive(not wants_emu68)
        self.boot_only_row.set_subtitle(
            "For a machine whose storage is elsewhere. The rest of the card "
            "is left unclaimed rather than formatted, so nothing asks to be "
            "initialised."
            if wants_emu68 else
            "Needs Emu68: a boot partition with no Emu68 on it and no Amiga "
            "drive either would be an empty card.")
        self.amiga_only_row.set_subtitle(
            "For a drive on the Amiga's own IDE or SCSI port - behind a "
            "PiStorm that boots from its own card, or a real accelerator. The "
            "Rigid Disk Block starts at block 0, where the controller looks "
            "for it, and there is no FAT32 partition."
            if not wants_emu68 else
            "Turn off \u201cInstall Emu68\u201d on the Source page first - "
            "Emu68 needs the boot partition this would remove.")

        #  With no boot partition there is nowhere to put a Kickstart, a
        #  config.txt or a cmdline.txt, so the settings that only exist there
        #  are taken off the window rather than left to be filled in and
        #  silently dropped.
        amiga_only = self.amiga_only_row.get_active()
        if getattr(self, "group_kickstart", None) is not None:
            self.group_kickstart.set_visible(not amiga_only)
        self.rewrite_group.set_visible(rebuilding)
        if rebuilding and not self._rewrite_drives:
            self._refresh_rewrite_drives()
        self.device_group.set_visible(self._writing_to_device())
        self.device_group.set_description(
            "Only removable drives are listed. Only the drive chosen below is "
            "written; the rest of the card is left as it is." if rebuilding
            else "Only removable drives are listed. Everything on the chosen "
                 "card will be destroyed.")
        self.file_group.set_visible(not self._writing_to_device())
        self.file_group.set_title("Amiga hard disk image" if making_hdf
                                  else "Image file")
        self.file_group.set_description(
            "A bare Amiga drive with a Rigid Disk Block and no boot partition - "
            "usable here, and in WinUAE or FS-UAE." if making_hdf
            else "The card image or .hdf that already has the drive." if rebuilding
            else "A sparse .img file you can write to a card later, or use with "
                 "an emulator.")
        partitions_ours = mode in (builder.BuildMode.FRESH, builder.BuildMode.HDF)
        self.file_size_row.set_visible(not self._writing_to_device() and partitions_ours)
        #  A card names its own size in this title; it is only reset where
        #  the size is the user's to type.
        if self._selected_device() is None:
            self.file_size_row.set_title(
                "Drive size" if making_hdf
                else "Image size - 32GB as cards are sold, 32GiB binary")
        #  No boot partition means no size to choose for one.
        self.boot_group.set_visible(partitions_ours and not making_hdf
                                    and not self.amiga_only_row.get_active())
        self._apply_task_rules()
        self._update_summary()

    def _apply_task_rules(self) -> None:
        """What the chosen task rules out, taken off the window.

        The switches the task decides are never shown; the groups a task has
        no use for are hidden on the pages it does show.
        """
        task = self._task
        self.mode_group.set_visible(False)
        self.boot_only_group.set_visible(False)
        self.install_emu_row.set_visible(task is not None
                                         and task.emu68 is None)
        self.drives_group.set_visible(task is builder.Task.SPLIT)
        if task is None:
            return
        self.device_group.set_title("The Pi's boot card"
                                    if task is builder.Task.SPLIT
                                    else "SD card")
        self._drives_target_changed()
        fills = task.fills_drives and (task is not builder.Task.REBUILD
                                       or self._rewrite_boots())
        self.group_primary.set_visible(fills)
        self.os_cd_group.set_visible(fills and self._system_source() == "cd")
        #  The floppy group's rows follow whether floppies are wanted; with
        #  none of them shown its heading stood on its own over nothing.
        self.os_group.set_visible(fills and self.volume_row.get_visible())
        #  The partition layout is the task's to make only on a new card or
        #  drive; elsewhere the drives come from what is being written.
        self.group_sizes.set_visible(task in (builder.Task.NEW_CARD,
                                              builder.Task.AMIGA_DRIVE))
        #  A PiStorm is the processor wherever Emu68 is written; the
        #  question is only asked of a drive that goes elsewhere.
        self.quick_accelerator.set_visible(not task.writes_boot_partition)
        self.quick_accelerator_cpu.set_visible(
            not task.writes_boot_partition
            and self._accelerator() is machines.Accelerator.ACCELERATOR)
        #  The Kickstart is Emu68's where there is a boot partition to put it
        #  on, and where WHDLoad's images come from where drives are filled.
        self.group_kickstart.set_visible(task.writes_boot_partition
                                         or task.fills_drives)
        self.whdload_rom_group.set_visible(task.fills_drives)

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
        #  The list arrives from GitHub after the window is up, so the summary
        #  was written while there was no build to offer and went on saying
        #  "Still needed: an Emu68 release" long after one had been chosen.
        self._update_summary()
        return False

    def _releases_failed(self, message: str) -> bool:
        self.release_row.set_model(combo(["Could not reach GitHub"]))
        self.release_row.set_subtitle(
            f"{message}. Choose a local Emu68 zip below instead.")
        self._update_summary()
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

    def _boot_only_changed(self) -> None:
        """A card with no Amiga drive has nothing to lay out or fill.

        The rows are hidden rather than cleared, so turning the switch off
        again brings back exactly the layout that was there - the answer to
        "how big is DH0" should not be destroyed by asking a different
        question.
        """
        only = self.boot_only_row.get_active()
        for group in ("partition_group", "expand_group"):
            widget = getattr(self, group, None)
            if widget is not None:
                widget.set_visible(not only)
        #  Nothing here has to reach across to the other switch: Emu68
        #  allows one or the other, never both, and _sync_visibility is the
        #  single place that decides which.
        self._update_summary()

    def _amiga_only_changed(self) -> None:
        """Drives and no boot partition, which decides Emu68 with it.

        Emu68 lives on the FAT32 boot partition, so a card without one cannot
        carry it and a card with one is not worth building without it. Rather
        than let the two disagree, the Emu68 switch follows this one and says
        why it cannot be operated - a switch that can only produce a card that
        does not boot is worse than no switch.
        """
        self._sync_visibility()

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

    def _apply_suggested_packages(self) -> None:
        """Tick the set that suits the machine and screen that are chosen."""
        wanted = set(packages.suggested(
            self._machine(), self._display(),
            #  ssid_row, not wifi_ssid: there is no such widget, so pressing
            #  the button raised AttributeError inside the signal handler and
            #  did nothing at all, quietly.
            networking=bool(self.ssid_row.get_text().strip()),
            #  The Raspberry Pi side of the setup, so a suggestion cannot
            #  offer software the board cannot run.
            pi=self._pi(),
            cpu=self._machine().cpu_fitted(self._accelerator(),
                                           self._accelerator_cpu()),
            emu68_tag=self._release_tag()))
        #  A whole set arriving at once is the suggestion being taken, not a
        #  person weighing one package against another; asking about each
        #  clash inside it would be a queue of dialogs answering nothing.
        was = getattr(self, "_settling_packages", False)
        self._settling_packages = True
        try:
            for key, row in self.package_rows.items():
                if row.get_sensitive():
                    row.set_active(key in wanted)
                elif not packages.CATALOGUE_BY_KEY[key].essential:
                    #  Refused here, and on only because something ticked
                    #  earlier dragged it in: the suggestion lets it go.
                    row.set_active(False)
        finally:
            self._settling_packages = was
        self._tick_what_is_needed()
        self._refresh_packages()
        #  The three drive lists are all driven by which packages are on, so
        #  taking the suggestion has to bring them with it. Without this the
        #  older-copies list still described the set that was ticked before.
        self._refresh_older_copies()
        self._refresh_what_cannot_work()
        self._refresh_clutter()
        self._refresh_desktop()
        self._refresh_what_arrives()

    def _refresh_categories(self) -> None:
        """Re-default every partition's categories for the machine now chosen."""
        for row in list(self.partition_rows) + list(self.extra_rows):
            row.reload_categories()

    def _older_copies_on_the_drive(self) -> dict[str, tuple[str, str]]:
        """Copies of chosen software already on the drive, somewhere else.

        Discovered, never declared. The names come from the archives the
        chosen packages install, and the drive is searched for them - so this
        works on any prepared drive rather than only on the one distribution
        somebody checked by hand. Each answer is (drawer, what to say).
        """
        found: dict[str, tuple[str, str]] = {}
        path = getattr(getattr(self, "quick_hdf", None), "path", "")
        chosen = self._chosen_packages()
        if not path or not chosen:
            return found
        try:
            from ..core import amigaos, content, packages as _p  # noqa: PLC0415
            wanted, filling = self._principal(chosen)
            if not wanted:
                return found
            reader, _label = amigaos.open_amiga_volume(path, "")
        except Exception:                                    # noqa: BLE001
            return found
        try:
            #  The walk is the slow part, so it is kept and reused: without
            #  that, every tick of a package would search the drive again.
            if getattr(self, "_scanned_drive", None) != path:
                self._scanned_drive = path
                self._drive_listing = content.list_files(reader)
            for copy in content.find_duplicates(reader, wanted, filling,
                                                self._drive_listing):
                shown = (f"{copy.label} {_version(copy.theirs)} is in "
                         f"{copy.drawer}; you chose {_version(copy.ours)}")
                #  Say which way round it is. "The same version, or one that
                #  cannot be read" covered three different situations and was
                #  wrong about at least one of them: ClassicWB's Scalos is
                #  39.222 against the package's 39.218, so the drive has the
                #  newer copy and removing it would be a downgrade.
                if copy.ours and copy.theirs and copy.theirs > copy.ours:
                    shown += " - the drive's copy is the newer one"
                elif copy.ours and copy.theirs and copy.theirs == copy.ours:
                    shown += " - the same version either way"
                elif not copy.certain:
                    shown += " - the versions cannot be compared"
                found[copy.drawer] = (shown, "sure" if copy.certain else "ask")
        except Exception:                                    # noqa: BLE001
            return found
        finally:
            try:
                reader.f.close()
            except Exception:                                # noqa: BLE001
                pass
        return found

    def _refresh_what_arrives(self) -> None:
        """List the programs the chosen drive already carries."""
        if not hasattr(self, "arrives_group"):
            return
        path = getattr(getattr(self, "quick_hdf", None), "path", "")
        found: list[tuple[str, str]] = []
        if path:
            try:
                from ..core import amigaos, content          # noqa: PLC0415
                reader, _label = amigaos.open_amiga_volume(path, "")
            except Exception:                                # noqa: BLE001
                reader = None
            if reader is not None:
                try:
                    found = self._installed_on_the_drive(path, reader)
                finally:
                    try:
                        reader.f.close()
                    except Exception:                        # noqa: BLE001
                        pass
        #  Not what another list is already dropping. FMSsys was in both - on
        #  here meaning "keep it", on there meaning "remove it" - so the page
        #  said two opposite things about the same program, and the one that
        #  read as keeping it was the longer list. That was fixed for the
        #  "cannot work" list alone, and the older copies and the clutter
        #  behave the same way: AWeb was offered for removal as an older copy
        #  and listed as arriving from the drive at the same time.
        removing = self._being_removed()
        wanted = {f"{drawer}/{name}" for drawer, name in found
                  if not self._covered_by(f"{drawer}/{name}", removing)}
        for key, row in list(self.arrives_rows.items()):
            if key not in wanted:
                self.arrives_group.remove(row)
                del self.arrives_rows[key]
        for drawer, name in found:
            key = f"{drawer}/{name}"
            if key in self.arrives_rows or key not in wanted:
                continue
            row = Adw.SwitchRow(title=name, subtitle=f"in {drawer}")
            row.set_active(True)                 # keep it, unless told not to
            row.connect("notify::active", lambda *_a: self._decisions_changed())
            self.arrives_rows[key] = row
            self.arrives_group.add(row)
        self.arrives_group.set_visible(bool(self.arrives_rows))

    def _refresh_what_cannot_work(self) -> None:
        """List software the drive carries that this card cannot run."""
        if not hasattr(self, "broken_group"):
            return
        path = getattr(getattr(self, "quick_hdf", None), "path", "")
        found = []
        if path:
            try:
                from ..core import amigaos, content            # noqa: PLC0415
                reader, _label = amigaos.open_amiga_volume(path, "")
            except Exception:                                  # noqa: BLE001
                reader = None
            if reader is not None:
                try:
                    named = [spec.volume_name or spec.name
                             for spec in (row.spec() for row
                                          in getattr(self, "partition_rows", []))]
                    volumes = content.volumes_on_the_card(reader, named)
                    drivers = []
                    entry = reader.find("Devs/DOSDrivers")
                    if entry is not None and entry.is_dir:
                        drivers = [e.name for e in
                                   reader.listdir(content._locator(entry))]
                    found = content.cannot_work(reader, volumes, drivers)
                except Exception:                              # noqa: BLE001
                    found = []
                finally:
                    try:
                        reader.f.close()
                    except Exception:                          # noqa: BLE001
                        pass
        wanted = {b.drawer: b for b in found}
        for key, row in list(self.broken_rows.items()):
            if key not in wanted:
                self.broken_group.remove(row)
                del self.broken_rows[key]
        for drawer, broken in wanted.items():
            if drawer in self.broken_rows:
                continue
            row = Adw.SwitchRow(title=f"Remove {drawer}",
                                subtitle="; ".join(broken.reasons))
            row.set_active(True)
            row.connect("notify::active", lambda *_a: self._decisions_changed())
            self.broken_rows[drawer] = row
            self.broken_group.add(row)
        self.broken_group.set_visible(bool(self.broken_rows))

    def _refresh_clutter(self) -> None:
        """Offer what the drive carries that this card would be better without.

        Every candidate is discovered - an empty drawer, a drawer of emulator
        scripts, an assign whose target is being left out - and every one is
        offered rather than acted on, because a drawer goes whole.
        """
        if not hasattr(self, "clutter_group"):
            return
        path = getattr(getattr(self, "quick_hdf", None), "path", "")
        found = []
        if path:
            try:
                from ..core import amigaos, content            # noqa: PLC0415
                reader, _label = amigaos.open_amiga_volume(path, "")
            except Exception:                                  # noqa: BLE001
                reader = None
            if reader is not None:
                try:
                    named = [spec.volume_name or spec.name
                             for spec in (row.spec() for row
                                          in getattr(self, "partition_rows", []))]
                    #  What the packages are about to fill is never offered: a
                    #  drawer empty now is not empty on the finished card.
                    _wanted, filling = self._principal(
                        self._chosen_packages())
                    #  ...and whatever the Workbench disks will add. A drawer
                    #  empty on the drive being built from is not empty on the
                    #  finished card: ClassicWB ships Rexxc and Expansion with
                    #  nothing in them and the floppy install fills both, so
                    #  offering to remove one took Commodore's own files with
                    #  it and the card came out with no ARexx commands at all.
                    keep = set(filling)
                    if self.adf_row.path:
                        keep |= amigaos.drawers_on_the_disks(self.adf_row.path)
                    #  Libraries this build soft-kicks for itself. A
                    #  distribution's own installer for one of them is a
                    #  second, riskier route to something already done.
                    chosen = set(self._chosen_packages())
                    provided = [p.boot_library for p in packages.CATALOGUE
                                if p.boot_library and p.key in chosen]
                    found = content.clutter(
                        reader, content.volumes_on_the_card(reader, named),
                        keep=keep, going=self._already_leaving(),
                        provided=provided)
                except Exception:                              # noqa: BLE001
                    found = []
                finally:
                    try:
                        reader.f.close()
                    except Exception:                          # noqa: BLE001
                        pass
        wanted = {c.path: c for c in found}
        for key, row in list(self.clutter_rows.items()):
            if key not in wanted:
                self.clutter_group.remove(row)
                del self.clutter_rows[key]
                self._clutter_default.pop(key, None)
        for where, item in wanted.items():
            row = self.clutter_rows.get(where)
            if row is None:
                row = Adw.SwitchRow(title=f"Remove {where}")
                row.connect("notify::active",
                            lambda *_a: self._decisions_changed())
                self.clutter_rows[where] = row
                self.clutter_group.add(row)
                self._clutter_default[where] = None
            #  A row's reason and its default both depend on the rest of the
            #  page. Ticking the icon library turns "replaces
            #  S:Startup-Sequence to do its work" into "...to install
            #  icon.library, which this build already installs", and the
            #  answer from "ask" to "yes". Created once and then skipped, the
            #  row kept the wording and the switch it was born with, so the
            #  card went out still carrying the installer that had bricked one.
            row.set_subtitle(item.reason)
            #  On only where the evidence is conclusive - "almost empty" is a
            #  judgement, and the answer that keeps somebody's files is safe.
            #  An answer the user has given is never overwritten: the default
            #  moves only while the switch still sits where this put it.
            was = self._clutter_default.get(where)
            if was is None or row.get_active() == was:
                row.set_active(item.certain)
            self._clutter_default[where] = item.certain
        self.clutter_group.set_visible(bool(self.clutter_rows))

    def _refresh_desktop(self) -> None:
        """Offer each icon the drive keeps on the Workbench desktop."""
        if not hasattr(self, "desktop_group"):
            return
        path = getattr(getattr(self, "quick_hdf", None), "path", "")
        found = []
        if path:
            try:
                from ..core import amigaos, content            # noqa: PLC0415
                reader, _label = amigaos.open_amiga_volume(path, "")
            except Exception:                                  # noqa: BLE001
                reader = None
            if reader is not None:
                try:
                    found = content.desktop_icons(reader)
                except Exception:                              # noqa: BLE001
                    found = []
                finally:
                    try:
                        reader.f.close()
                    except Exception:                          # noqa: BLE001
                        pass
        wanted = {i.path: i for i in found}
        for key, row in list(self.desktop_rows.items()):
            if key not in wanted:
                self.desktop_group.remove(row)
                del self.desktop_rows[key]
        for where, icon in wanted.items():
            if where in self.desktop_rows:
                continue
            if icon.missing:
                why = "names something that is not on the drive"
            elif icon.reachable:
                why = f"also in {icon.reachable}, so the desktop copy is a shortcut"
            else:
                why = "only reachable from the desktop"
            row = Adw.SwitchRow(title=where, subtitle=why)
            #  On means "leave it on the desktop". Everything the drive chose
            #  to put there stays until somebody says otherwise; this is a
            #  preference, and the card works either way.
            row.set_active(True)
            row.connect("notify::active", lambda *_a: self._update_summary())
            self.desktop_rows[where] = row
            self.desktop_group.add(row)
        self.desktop_group.set_visible(bool(self.desktop_rows))

    def _principal(self, chosen: list[str]):
        """``packages.principal_programs`` for a set of ticks, remembered.

        It unpacks and reads every chosen archive - over a second on a full
        selection - and depends on nothing but the ticks, yet it was
        recomputed by both lists that use it on every refresh. With the lists
        now re-deriving each other whenever a switch moves, that turned each
        click into a multi-second pause.
        """
        key = tuple(sorted(chosen))
        if getattr(self, "_principal_key", None) != key:
            self._principal_key = key
            self._principal_value = packages.principal_programs(chosen)
        return self._principal_value

    def _installed_on_the_drive(self, path: str, reader):
        """What the drive carries, remembered per drive.

        The list shown is this filtered by what the rest of the page is
        dropping, and only the filter changes when a switch moves.
        """
        if getattr(self, "_installed_path", None) != path:
            self._installed_path = path
            self._installed_value = content.installed_programs(reader)
        return self._installed_value

    def _being_removed(self) -> list[str]:
        """Every path the removal lists are currently set to drop.

        The page shows one drive through several lists, and they describe the
        same facts: a drawer offered as an older copy is also a drawer the
        drive arrives with. Each list must therefore be able to see what the
        others have decided, and there can only be one answer to "is this
        going". Assembled separately, the page said two opposite things about
        the same program - AWeb switched on under "older copies", meaning
        remove it, and switched on under "already installed on the drive",
        meaning keep it - and moving either switch did nothing to the other.

        Nothing here names a program, a drawer or a distribution: the lists
        are matched by path, so this holds for whatever drive somebody starts
        from.
        """
        out: list[str] = []
        for rows in (getattr(self, "older_rows", {}),
                     getattr(self, "broken_rows", {}),
                     getattr(self, "clutter_rows", {})):
            out += [key for key, row in rows.items() if row.get_active()]
        return out

    @staticmethod
    def _covered_by(path: str, removing: Iterable[str]) -> bool:
        """Whether a path is one being dropped, or sits inside one.

        Equality is not enough: the lists need not agree on depth, and a
        program inside a drawer that is going is going with it.
        """
        low = path.strip("/").lower()
        return any(low == other or low.startswith(other + "/")
                   for other in (p.strip("/").lower() for p in removing)
                   if other)

    def _already_leaving(self) -> list[str]:
        """Paths the other lists are already dropping from the card.

        An assign is only broken by a removal if the removal is happening, so
        the clutter pass has to be told what the rest of the page has decided.
        """
        return [key for key, row in getattr(self, "arrives_rows", {}).items()
                if not row.get_active()] + self._being_removed()

    def _decisions_changed(self) -> None:
        """One list has been answered, so re-derive the ones that depend on it.

        Without this the tie is only as good as the moment a list was built:
        switching "remove this older copy" on left the same drawer still
        listed as arriving from the drive, and switching it off left it
        hidden. Re-entrant because refreshing a list moves switches, so it is
        fenced rather than left to chance.

        Only the list that can *contradict* another is re-derived here. The
        clutter pass is deliberately not: it is given ``_already_leaving()``,
        which includes its own switched-on rows, so re-running it on every
        click feeds it its own output and the removal count climbs with each
        one - six, then eight, then nine on a single drive. That loop is
        real work (dropping a drawer does break an assign to it) but it has
        to be run to a fixed point on purpose, not a step at a time by
        whoever last touched a switch. It keeps the triggers it had.
        """
        if getattr(self, "_settling_lists", False):
            return
        self._settling_lists = True
        try:
            self._refresh_what_arrives()
        finally:
            self._settling_lists = False
        self._update_summary()

    def _refresh_older_copies(self) -> None:
        """Show one row per older copy actually found, keeping any answers."""
        if not hasattr(self, "older_group"):
            return
        found = self._older_copies_on_the_drive()
        for drawer, row in list(self.older_rows.items()):
            if drawer not in found:
                self.older_group.remove(row)
                del self.older_rows[drawer]
        for drawer, (shown, how) in found.items():
            if drawer in self.older_rows:
                continue
            row = Adw.SwitchRow(title=f"Remove {drawer}", subtitle=shown)
            #  On only where the version it carries is provably older than
            #  the one being installed. Anything else is a question, and the
            #  answer that keeps somebody's software is the safe one.
            row.set_active(how == "sure")
            row.connect("notify::active", lambda *_a: self._decisions_changed())
            self.older_rows[drawer] = row
            self.older_group.add(row)
        self.older_group.set_visible(bool(self.older_rows))

    def _refresh_packages(self) -> None:
        """Offer the software that suits this machine and this screen.

        Everything in the list is fetched from its publisher, so what is on
        offer no longer depends on what some other installation happened to
        hold - only on whether it makes sense here.
        """
        if not self._ready:
            return
        self._refresh_overlay_options()
        display = self._display()
        chipset = self._machine().chipset
        pi = self._pi()
        cpu = self._machine().cpu_fitted(self._accelerator(),
                                         self._accelerator_cpu())
        tag = self._release_tag()
        for key, row in self.package_rows.items():
            package = packages.CATALOGUE_BY_KEY[key]
            fits = package.suits(chipset, display, pi=pi, cpu=cpu,
                                 emu68_tag=tag)
            note = package.description
            if not fits and package.rtg_only:
                note += "  -  only useful with an RTG display."
            elif not fits and package.native_only:
                note += "  -  only useful on the Amiga's own screen."
            #  Said before the chipset, because on a card refused for both
            #  reasons the Raspberry Pi is the one the user can do something
            #  about: the Amiga is what it is, the Pi and the Emu68 build are
            #  chosen here.
            elif not fits and package.pi_models \
                    and pi not in package.pi_models:
                wanted = " or ".join(model.label
                                     for model in package.pi_models)
                note += (f"  -  needs {wanted} on the board; this card is "
                         f"being built for {pi.label}.")
            elif not fits and package.min_emu68 \
                    and not emu68.at_least(tag or "", package.min_emu68):
                version = ".".join(str(part) for part in package.min_emu68)
                note += (f"  -  needs Emu68 {version} or newer; choose one on "
                         f"the Source page.")
            elif not fits and package.unsuited_need(
                    chipset, display, pi=pi, cpu=cpu, emu68_tag=tag):
                need = package.unsuited_need(chipset, display, pi=pi, cpu=cpu,
                                             emu68_tag=tag)
                note = (f"Needs {need.label}, which is not offered for this "
                        f"setup.  -  " + note)
            elif not fits:
                note += "  -  not a fit for this chipset."
            else:
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
            #  An essential package is part of the choice that brought it in,
            #  not an extra beside it: choosing an RTG display and then being
            #  handed a card with no RTG screen modes is not a choice anyone
            #  made. So it comes on with that display and cannot be dropped
            #  while it lasts.
            if fits and package.essential:
                was = getattr(self, "_settling_packages", False)
                self._settling_packages = True
                try:
                    row.set_active(True)
                finally:
                    self._settling_packages = was
                row.set_sensitive(False)
                #  First, not last. A switch that is on and cannot be moved
                #  is a question - "why can I not change this?" - and the
                #  answer was arriving at the end of three hundred characters
                #  of description and installation notes, where it was asked
                #  about rather than read.
                note = ("Required by the display you chose, so it is on and "
                        "cannot be turned off - change the display on the "
                        "Amiga page to release it.  -  " + note)
            else:
                row.set_sensitive(fits)
                if not fits:
                    row.set_active(False)
            row.set_subtitle(GLib.markup_escape_text(note))
        self._refresh_usb()
        self._on_layout_changed()

    def _chosen_addons(self) -> list[str]:
        return [key for key, row in getattr(self, "addon_rows", {}).items()
                if row.get_active()]

    def _addons_need_writable_boot(self) -> bool:
        """Whether anything chosen needs the Amiga to write to the boot partition."""
        return any(bootaddon.CATALOGUE_BY_KEY[key].writable_boot
                   for key in self._chosen_addons()
                   if key in bootaddon.CATALOGUE_BY_KEY)

    def _on_addon_toggled(self, _key: str) -> None:
        if getattr(self, "_settling_addons", False):
            return
        self._refresh_boot_addons()
        self._update_summary()

    def _refresh_boot_addons(self) -> None:
        """Offer what this card can actually take, and say why when it cannot.

        A switch that is simply greyed out is a question. The reason is known
        exactly here - the chipset, the Pi, the chip RAM, the Emu68 build - so
        it is given, in the row, rather than left to be guessed at.
        """
        if not getattr(self, "addon_rows", None):
            return
        machine = self._machine()
        accelerator = self._accelerator()
        pi = self._pi()
        chip_ram = self._chip_ram()
        tag = self._release_tag()
        for key, row in self.addon_rows.items():
            addon = bootaddon.CATALOGUE_BY_KEY[key]
            why = addon.refusal(machine, accelerator=accelerator, pi=pi,
                                chip_ram=chip_ram, emu68_tag=tag)
            note = addon.description
            if why:
                note = f"{addon.label} {why}  -  {note}"
            else:
                #  Where the archive is, or where to get one. Said before the
                #  build rather than in the log afterwards, which is the rule
                #  the manual packages already follow.
                found = bootaddon.find_archive(addon)
                note += (f"  -  will be taken from {found.name}."
                         if found is not None else
                         f"  -  no archive found. Download it from "
                         f"{addon.home} and put it in "
                         f"{packages.cache_dir()}, or anywhere this tool "
                         f"looks for Amiga material.")
            row.set_subtitle(GLib.markup_escape_text(note))
            was = getattr(self, "_settling_addons", False)
            self._settling_addons = True
            try:
                row.set_sensitive(not why)
                if why:
                    row.set_active(False)
            finally:
                self._settling_addons = was

        #  The boot partition has to be writable from the Amiga for an
        #  add-on's own installer to finish, so that switch comes on with it
        #  and is held there - the same way a display that needs Picasso96
        #  holds Picasso96 on. The switch still tells the truth, and gather()
        #  still reads the switch.
        needed = self._addons_need_writable_boot()
        if needed:
            was, self._ready = self._ready, False
            try:
                self.unit0_row.set_active(True)
            finally:
                self._ready = was
        self.unit0_row.set_sensitive(not needed)
        self.unit0_row.set_subtitle(
            "Held on by the boot partition add-on you chose: its installer "
            "runs on the Amiga and writes to this partition."
            if needed else self._unit0_subtitle)

    def _refresh_overlay_options(self) -> None:
        """Hold off the settings this Emu68 release has no way to be told.

        They exist only as device tree overlays, so on a release older than
        1.1 there is nothing to write them into. A switch that cannot reach
        the card must not be left looking as though it can - that is the whole
        complaint these pages have been built around.
        """
        tag = self._release_tag()
        #  An unknown release - a local zip, an unpacked folder, or the list
        #  not arrived yet - is allowed through rather than refused: nothing
        #  can read a version off it, and the build says what it could not
        #  honour. It is the same rule the packages page applies.
        supported = emu68.at_least(tag or "", (1, 1))
        self.overlay_group.set_sensitive(supported)
        self.overlay_group.set_description(
            "Settings that arrived with Emu68 1.1 and exist only as device "
            "tree overlays." if supported else
            "These arrived with Emu68 1.1, and there is no way to write them "
            "for an older release. Choose Emu68 1.1 or newer on the Source "
            "page to use them.")

    def _release_tag(self) -> str:
        """The Emu68 release the card will be built from, where one is known.

        Empty when the list has not arrived yet, or when the build is coming
        from a local zip or an unpacked folder - neither of which carries a
        version for anything to read.
        """
        choices = getattr(self, "_release_choices", [])
        if not choices or self.local_zip_row.path:
            return ""
        index = min(self.release_row.get_selected(), len(choices) - 1)
        return choices[max(index, 0)].tag

    def _usb_ports(self) -> list[machines.UsbPort]:
        """The USB sockets this card could be pointed at."""
        return list(machines.usb_ports(self._pi()))

    def _usb_port(self) -> machines.UsbPort | None:
        """The socket the card is being built for, or None when there is none.

        None whenever no chosen software needs one, so a card with no USB
        stack on it is not quietly told to turn its OTG socket into a host
        port - a config.txt line that changes what the hardware does, written
        for no reason at all.
        """
        ports = self._usb_ports()
        if not ports or not packages.wants_setting(self._chosen_packages(),
                                                   packages.USB_UNIT):
            return None
        index = min(self.usb_port_row.get_selected(), len(ports) - 1)
        return ports[max(index, 0)]

    def _refresh_usb(self) -> None:
        """Offer the sockets this Pi has, and only where something needs one."""
        ports = self._usb_ports()
        wanted = packages.wants_setting(self._chosen_packages(),
                                        packages.USB_UNIT)
        self.usb_group.set_visible(bool(ports) and wanted)
        if not ports:
            return
        labels = [port.label for port in ports]
        if labels != getattr(self, "_usb_labels", None):
            was, self._ready = self._ready, False
            try:
                self.usb_port_row.set_model(combo(labels))
                self.usb_port_row.set_selected(0)
            finally:
                self._ready = was
            self._usb_labels = labels
        port = self._usb_port()
        self.usb_port_row.set_subtitle(
            "config.txt is given otg_mode=1 so this socket becomes a host "
            "port." if port is not None and port.needs_otg_mode
            else "The four USB-A sockets are unit 1 of the driver, which is "
                 "what the startup line will say.")

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
        #  A question only worth asking when something that needs the answer
        #  is on, so it appears and disappears with the tick that wants it.
        self._refresh_usb()
        #  After the settling, never during it: what the drive already
        #  carries follows from the final set of packages, not from each
        #  intermediate state as dependencies are switched on and off.
        self._refresh_older_copies()
        if row is not None and row.get_active():
            self._ask_about_rivals(key)

    def _rivals(self, key: str) -> list[str]:
        """Anything switched on that does the same job as ``key``."""
        package = packages.CATALOGUE_BY_KEY.get(key)
        if package is None or not package.role:
            return []
        return [other for other, row in self.package_rows.items()
                if other != key and row.get_active()
                and packages.CATALOGUE_BY_KEY[other].role == package.role]

    def _asking_is_welcome(self) -> bool:
        """Whether a question about the software would make any sense now.

        Only when somebody is looking at the page the choice lives on. Rows
        are also set by restoring a saved setup, by the suggested load and by
        the display forcing Picasso96 on - and a question about two icon sets
        arriving over the quick start, in answer to nothing the person did,
        is a interruption rather than a choice.
        """
        if getattr(self, "_settling_packages", False):
            return False
        return self.stack.get_visible_child_name() == "packages"

    def _ask_about_rivals(self, key: str) -> None:
        """Two packages doing one job is rarely what anybody means.

        Asked rather than decided: they patch the same part of the system and
        the answer is usually to drop the older choice, but somebody may want
        both and it is not this tool's place to overrule them.
        """
        if not self._asking_is_welcome():
            return
        rivals = self._rivals(key)
        if not rivals:
            return
        package = packages.CATALOGUE_BY_KEY[key]
        others = ", ".join(packages.CATALOGUE_BY_KEY[r].label for r in rivals)
        dialog = Adw.AlertDialog(
            heading=f"{others} does the same job",
            body=(f"{package.label} and {others} are both a {package.role} "
                  f"system, and they patch the same part of Workbench. "
                  f"Would you like {others} taken off the card?"))
        dialog.add_response("keep", "Keep both")
        dialog.add_response("remove", f"Remove {others}")
        dialog.set_response_appearance("remove",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("remove")
        dialog.set_close_response("keep")

        def answered(_dialog, response) -> None:
            if response != "remove":
                return
            was = getattr(self, "_settling_packages", False)
            self._settling_packages = True
            try:
                for other in rivals:
                    self.package_rows[other].set_active(False)
            finally:
                self._settling_packages = was
            self._toast(f"{others} removed")
            self._on_layout_changed()

        dialog.connect("response", answered)
        dialog.present(self)

    def _chosen_packages(self) -> list[str]:
        #  Not "and sensitive": a package the display makes essential is
        #  ticked and locked, and testing sensitivity here dropped it back
        #  out of the build it had just been forced into.
        return [key for key, row in self.package_rows.items()
                if row.get_active()]

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
        if hasattr(self, "drives_device_row"):
            self.drives_device_row.set_model(combo(labels))
            self.drives_device_row.set_selected(0)
        self._update_summary()

    def _known_card_size(self) -> int:
        """The card size if one has been said, for a "is it big enough" check."""
        try:
            return parse_size(self.file_size_row.get_text())
        except Exception:                        # noqa: BLE001 - not set yet
            return 0

    def _on_image_chosen(self) -> None:
        from ..core import imgsrc
        if not self.image_row.path:
            self.image_info.set_subtitle("No image selected")
            self.hdf_row.set_path("")
            self._update_summary()
            return
        try:
            source = imgsrc.inspect(self.image_row.path)
            description = source.description
            kind = builder.image_kind(self.image_row.path)
        except Exception as error:  # noqa: BLE001
            self.image_info.set_subtitle(f"Cannot read this file: {error}")
            self._update_summary()
            return
        self._image_is(kind)
        if kind is builder.ImageKind.DRIVE:
            self.image_info.set_subtitle(
                "An Amiga drive - " + description + "\nAn Emu68 boot "
                "partition is built around it.")
            self._update_summary()
            return
        if kind is None:
            description += ("\nNeither a partition table nor an Amiga drive "
                            "was found at its start; it will be written as "
                            "it is.")
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

    def _image_is(self, kind: "builder.ImageKind | None") -> None:
        """Turn the image task into the one the chosen file needs.

        A drive is put on the card with a boot partition built around it, a
        card - or a file that says neither - is written as it is.
        """
        if self._task not in IMAGE_TASKS.values():
            return
        task = IMAGE_TASKS.get(kind, builder.Task.PREPARED)
        self.hdf_row.set_path(self.image_row.path
                              if task is builder.Task.DRIVE_IMAGE else "")
        if task is self._task:
            return
        self._task = task
        was, self._ready = self._ready, False
        try:
            for index, entry in enumerate(MODES):
                if entry[1] is task.mode:
                    self.mode_row.set_selected(index)
                    break
            if task.emu68 is not None:
                self.install_emu_row.set_active(task.emu68)
        finally:
            self._ready = was
        self._sync_visibility()
        self._relayout_partitions()

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

    def _scan_whdload_roms(self) -> None:
        """Say which ROMs WHDLoad will be given, off the UI thread.

        Its relocation tables say which Kickstarts it can use, and they come
        from a download, so this cannot hold the window up.
        """
        folder = self.whdload_rom_row.path or (
            str(Path(self.rom_row.path).parent) if self.rom_row.path else "")
        if not folder:
            self.whdload_rom_info.set_subtitle(
                "Choose a folder, or a Kickstart above")
            return
        key = None if (Path(folder) / "rom.key").exists() \
            else (self.rom_key_row.path or None)
        self.whdload_rom_info.set_subtitle("Looking…")

        def work() -> None:
            try:
                tables = packages.whdload_tables()
                found = kickstart.whdload_images(folder, tables, key)
                if not tables:
                    text = ("WHDLoad's relocation tables could not be "
                            "fetched; this is checked again when the card "
                            "is built")
                elif found:
                    text = ", ".join(f"{name} ({info.path.name})"
                                     for name, _data, info in found)
                else:
                    text = (f"None of the ROMs in {Path(folder).name} is one "
                            f"WHDLoad can use - 1.3 (34.5) is the one most "
                            f"games want")
            except Exception as error:  # noqa: BLE001 - said, not raised
                text = f"Could not look: {error}"
            GLib.idle_add(self.whdload_rom_info.set_subtitle, text)

        threading.Thread(target=work, daemon=True).start()

    def _on_rom_chosen(self) -> None:
        #  The WHDLoad folder follows the Kickstart's own until one is chosen.
        if hasattr(self, "whdload_rom_row") and not self.whdload_rom_row.path:
            self._scan_whdload_roms()
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
        if info.version and info.aga is False:
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
        #  Before anything can return early: the button names the task, not
        #  the state of it, and an unfinished export was still offering to
        #  "Write card".
        self.write_button.set_label(
            "Export" if self._mode() is builder.BuildMode.EXPORT
            else "Write card")
        missing = self._missing_choices()
        if hasattr(self, "concerns_group"):
            self.concerns_group.set_visible(False)
        if hasattr(self, "missing_group"):
            self.missing_group.set_visible(bool(missing))
            self.missing_label.set_text(
                "\n".join(f"\u2022 {item[0].upper()}{item[1:]}"
                          for item in missing))
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
        target = config.target
        #  Exporting writes files out of an image and touches no card, so the
        #  button that says "Write card" is the wrong promise, the target is
        #  the folder rather than a card, and there is no setup to apply
        #  first - reading drives out of an image cannot destroy anything.
        if config.mode is builder.BuildMode.EXPORT:
            drives = config.export_drives
            names = ", ".join(drives)
            problems = config.validate()
            if problems:
                self.summary.set_text(problems[0])
                self.write_button.set_sensitive(False)
                return
            self.summary.set_text(
                f"Export {len(drives)} drive(s) - {names} - from "
                f"{Path(config.source_image).name} \u2192 {config.export_dir}")
            self.write_button.set_sensitive(True)
            return
        #  One line per task. This was two if-chains, and the second always
        #  ran: a prepared image read "Update the boot partition of".
        task = config.task
        what = {
            builder.Task.PREPARED: f"Write {Path(config.source_image).name} to",
            builder.Task.DRIVE_IMAGE: f"Build a card around "
                                      f"{Path(config.hdf_image).name} on",
            builder.Task.NEW_CARD: "Partition and build",
            builder.Task.BOOT_CARD: "Write an Emu68 boot card to",
            builder.Task.AMIGA_DRIVE: "Build Amiga drives on",
            builder.Task.REBUILD: f"Rebuild {config.rewrite_drive} on",
            builder.Task.UPDATE: "Update the boot partition of",
        }.get(task, "Write")
        #  Choices that will build and probably are not what was meant: said
        #  here, where the setup is accepted, rather than discovered on the
        #  Amiga afterwards.
        concerns = config.concerns()
        self.concerns_group.set_visible(bool(concerns))
        self.concerns_label.set_text(
            "\n\n".join(f"\u2022 {c}" for c in concerns))
        note = ""
        if concerns:
            count = len(concerns)
            note = (f"  ·  {count} thing{'s' if count > 1 else ''} worth "
                    f"checking on the Review step")
        self.summary.set_text(f"{what} → {target}{note}")
        self.write_button.set_sensitive(True)
        self._quick_preview()

    # ------------------------------------------------------- config gather

    @staticmethod
    def _as_markup(text: str) -> str:
        """Escape text that is about to become a row's title or subtitle.

        Adwaita rows take Pango markup, so an ampersand in a label is not
        text - it starts an entity, GTK refuses the whole string, and the row
        keeps whatever it said before.  That is how "Updates found" went on
        reading "No folder selected" with a folder plainly selected: the pack
        is called "BoingBags 3 & 4".
        """
        return GLib.markup_escape_text(text)

    def _suit_the_rom_to_the_release(self) -> None:
        """Swap an auto-chosen Kickstart for one the release can run.

        Detection happens at startup, before any CD has been named, so it has
        no release to go on and prefers the newest ROM it can see. That is the
        wrong answer for 3.5 and 3.9, which need Kickstart 3.1 and refuse 3.2
        - so somebody with both ROMs was handed the one their release cannot
        use, and then told the build could not go ahead.

        Only a ROM this application chose is replaced. One that was picked by
        hand is left exactly where it is, and the build says plainly if it
        will not do.
        """
        release = self._os_cd_release()
        detected = getattr(self, "detected", None)
        found = detected.kickstart if detected is not None else None
        if not release or not hasattr(self, "rom_row"):
            return
        chosen = self.rom_row.path
        ours = {str(found.path)} if found else set()
        ours.add(getattr(self, "_rom_from_disc", ""))
        if chosen and chosen not in ours:
            return                          # theirs, not ours
        #  A disc that carries a Kickstart of its own is the better answer
        #  wherever the Kickstart is a file - which is a PiStorm, where Emu68
        #  loads it.  A machine running from the chip on its board has the
        #  ROM it has, and is not offered one it would have to be fitted with.
        carried = None
        if self._accelerator() is machines.Accelerator.PISTORM:
            carried = amigacd.kickstart_on_disc(
                self._os_cd_match, self._machine(), self._kickstart_cache())
        if carried is not None:
            self._rom_from_disc = str(carried.path)
            if chosen != self._rom_from_disc:
                self.rom_row.set_path(self._rom_from_disc)
                self._on_rom_chosen()
                self._toast(f"Using {carried.name}, from the disc")
            return
        roms = [r for r in kickstart.scan(found.path.parent) if r.usable] \
            if found is not None else []
        better = presets.best_rom(roms, release)
        if better is None:
            #  Nothing to put in its place, but a disc's Kickstart that no
            #  longer applies must not stay as though it did.
            if chosen and chosen == getattr(self, "_rom_from_disc", ""):
                self.rom_row.set_path("")
                self._on_rom_chosen()
            return
        if str(better.path) == chosen:
            return
        self.rom_row.set_path(str(better.path))
        self._on_rom_chosen()
        self._toast(f"Using {better.name} - AmigaOS {release} needs "
                    + amigacd.kickstart_wanted(
                        amigacd.RELEASES_BY_KEY[release]))

    @staticmethod
    def _kickstart_cache() -> Path:
        """Where a Kickstart lifted off a disc is kept."""
        return emu68.cache_dir() / "kickstart"

    def _os_cd_usable(self) -> bool:
        match = getattr(self, "_os_cd_match", None)
        return bool(match and match.release and match.usable)

    def _os_cd_release(self) -> str:
        match = getattr(self, "_os_cd_match", None)
        return match.release.key if match and match.release else ""

    def _os_cd_chosen_options(self) -> list[str]:
        """What was answered, of the questions the chosen disc asks.

        Only the chosen disc's: a switch left on for a disc that is no longer
        the one selected must not follow the build to a release that never
        asked.
        """
        match = getattr(self, "_os_cd_match", None)
        if not match or not match.release:
            return []
        return [option.key for option in match.release.options
                if self.os_cd_options[option.key].get_active()]

    def _show_os_cd_options(self) -> None:
        match = getattr(self, "_os_cd_match", None)
        asked = {option.key for option in match.release.options} \
            if match and match.release else set()
        for key, row in self.os_cd_options.items():
            row.set_visible(key in asked)

    def _boingbag_archives(self) -> list[str]:
        """Every .lha in the chosen folder, for the builder to sort out.

        Which packs are in which archive is the builder's question, not this
        one's: BB1-4.lha holds three of them and BoingBag39-1.lha holds one.
        """
        folder = self.boingbag_row.path
        if not folder or not Path(folder).is_dir():
            return []
        return sorted(str(item) for item in Path(folder).glob("*.lha"))

    def _chosen_boingbags(self) -> list[str]:
        """The packs to apply, for the release that was chosen.

        Empty would mean "every pack that is on by default", which is not the
        same thing once the community pack has a switch of its own - so the
        list is always explicit.
        """
        release = self._os_cd_release()
        if not release:
            return []
        wanted = []
        for bag in boingbag.for_release(release):
            if bag.official or self.boingbag_community.get_active():
                wanted.append(bag.key)
        return wanted

    def _on_os_cd_chosen(self) -> None:
        path = self.os_cd_row.path
        self._os_cd_match = None
        if not path or not Path(path).is_file():
            self.os_cd_details.set_subtitle("No CD selected")
            self._show_os_cd_options()
            self._update_summary()
            return
        match = amigacd.identify(path)
        self._os_cd_match = match if match.release else None
        self.os_cd_details.set_subtitle(self._as_markup(match.label))
        self._show_os_cd_options()
        self._suit_the_rom_to_the_release()
        self._on_boingbags_chosen()
        self._update_summary()

    def _on_boingbags_chosen(self) -> None:
        archives = self._boingbag_archives()
        release = self._os_cd_release()
        if not archives:
            self.boingbag_found.set_subtitle(
                "No folder selected" if not self.boingbag_row.path
                else "No .lha archives in that folder")
            self._update_summary()
            return
        if not release:
            self.boingbag_found.set_subtitle(self._as_markup(
                f"{len(archives)} archive(s) - choose a CD to say which "
                f"release they belong to"))
            self._update_summary()
            return
        names = [bag.label for bag in boingbag.for_release(release)]
        if not names:
            self.boingbag_found.set_subtitle(self._as_markup(
                f"AmigaOS {release} has no BoingBags, so nothing in that "
                f"folder is applied"))
            self._update_summary()
            return
        self.boingbag_found.set_subtitle(self._as_markup(
            f"{len(archives)} archive(s) for AmigaOS {release}: "
            + ", ".join(names)))
        self._update_summary()

    def gather(self, require_target: bool = True) -> builder.BuildConfig:
        """The job the window describes.

        ``require_target`` False describes it before anywhere to write it has
        been chosen, so the Review step can say what the build would be and
        what is still missing, rather than only that no card is selected.
        """
        mode = self._mode()
        if self._writing_to_device():
            index = self.device_row.get_selected() - 1   # row 0 is the placeholder
            if not self.device_list or index < 0 or index >= len(self.device_list):
                if require_target:
                    raise ValueError("No SD card selected.")
                target, is_device = "", True
            else:
                target, is_device = self.device_list[index].path, True
        else:
            target, is_device = self.file_row.path, False

        hdmi = bootcfg.HDMI_MODES[self.hdmi_row.get_selected()]
        overclock = {0: None, 1: True, 2: False}[self.overclock_row.get_selected()]
        antenna = {0: None, 1: True, 2: False}[self.antenna_row.get_selected()]
        vc4 = int(self.vc4_row.get_value())
        #  The USB socket decides two things that have to agree, and they are
        #  written in two different files: the unit number on the
        #  AddUSBHardware line in S:User-Startup, and whether config.txt turns
        #  the onboard OTG socket into a host port. Setting one and not the
        #  other is the shape of mistake this whole page is careful about, so
        #  both come from here, from the same widget.
        usb_port = self._usb_port()

        options = bootcfg.BootOptions(
            hdmi_group=hdmi[1], hdmi_mode=hdmi[2],
            hdmi_automatic=hdmi[1] is None,
            overclock=overclock,
            cm4_external_antenna=antenna,
            #  None, not False, where there is no USB: leaving the key alone
            #  keeps whatever the Emu68 release shipped, which is what every
            #  other untouched setting on this page does.
            otg_mode=(True if usb_port is not None and usb_port.needs_otg_mode
                      else None),
            vc4_mem=vc4 or None,
            vbr_move=self.vbr_row.get_active(),
            chip_slowdown=self.slowdown_row.get_active(),
            dbf_slowdown=self.dbf_row.get_active(),
            blitwait=self.blitwait_row.get_active(),
            swap_df0_with_df1=self.swapdf_row.get_active(),
            #  The switch, which an add-on holds on while it is chosen - so
            #  reading the switch is reading the answer either way. The builder
            #  applies the same rule, for a build driven from a saved job or
            #  the command line where there is no switch at all.
            sd_unit0_rw=self.unit0_row.get_active(),
            #  No switch decides this - the machine does - and it was set only
            #  where a quick setup was assembled, never here, where the card
            #  is actually written from. So every card went out without
            #  enable_c0_slow, and move_slow_to_chip had nothing to move: a
            #  machine told to give Workbench a megabyte of chip RAM came up
            #  with 512K.
            enable_slow_ram=machines.wants_slow_ram(self._machine()),
            #  Same story for the framethrower overlay: the display decides
            #  it, no widget does, and it was set only where a quick setup is
            #  assembled - so choosing Framethrower and writing from the
            #  pages produced a card with no overlay to drive it.
            unicam=machines.wants_unicam(self._display()),
            unicam_smooth=machines.wants_unicam(self._display()),
            no_ide=self.noide_row.get_active(),
            video_standard=bootcfg.VIDEO_STANDARDS[
                min(self.video_row.get_selected(),
                    len(bootcfg.VIDEO_STANDARDS) - 1)],
            jit_cache_mb=int(self.jit_row.get_value()) or None,
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

        rebuilding = mode is builder.BuildMode.REWRITE
        splitting = self._task is builder.Task.SPLIT
        drives_target, drives_on_card, drives_size = (
            self._drives_target() if splitting else ("", False, 8 * GIB))
        return builder.BuildConfig(
            mode=mode,
            target=target,
            drives_target=drives_target,
            drives_target_is_device=drives_on_card,
            drives_image_size=drives_size,
            target_is_device=is_device,
            image_size=image_size,
            variant=emu68.VARIANTS[self.variant_row.get_selected()].key,
            release_tag=release_tag,
            kernel_key=(self._chosen_kernel().key
                        if self._chosen_kernel() else ""),
            emu68_archive=self.local_zip_row.path,
            install_emu68=(self.install_emu_row.get_active()
                           and not self._making_hdf()),
            #  Export reads its own image, chosen on its own page: the
            #  Source page belongs to the builds and must not be borrowed.
            source_image=(self.export_source.path
                          if self._mode() is builder.BuildMode.EXPORT
                          else self.image_row.path
                          if self._mode() is builder.BuildMode.IMAGE
                          else ""),
            hdf_image=(self.hdf_row.path
                       if self._mode() is builder.BuildMode.HDF else ""),
            output_hdf=self._making_hdf(),
            #  Export reads an image and writes files; it shares the job, the
            #  progress and the button with the builds, and nothing else.
            export_drives=[name for name, row in
                           getattr(self, "export_rows", {}).items()
                           if row.get_active()],
            export_dir=getattr(getattr(self, "export_dir", None), "path", "") or "",
            repair_rdb=self.repair_row.get_active(),
            patch_display=self.patch_display_row.get_active(),
            boot_size=boot_size,
            boot_only=self.boot_only_row.get_active(),
            amiga_only=self.amiga_only_row.get_active(),
            rewrite_drive=(self._rewrite_drive_name() if rebuilding else ""),
            #  The rows are kept while the switch is on, so the layout
            #  survives being asked a different question - but they must not
            #  reach a card that is not going to have them.
            amiga_partitions=(self._rewrite_spec() if rebuilding
                              else [] if self.boot_only_row.get_active()
                              else [row.spec() for row in self.partition_rows]),
            pfs3_binary=self.quick_donor.path,
            #  Only a card we are partitioning ourselves can have an OS
            #  installed onto it from floppies - but a drive imported onto
            #  such a card may need them as well. ClassicWB's brings no
            #  Workbench of its own, and a card made from it alone stops at a
            #  Shell, so the two go together rather than one excluding the
            #  other.
            install_amigaos=((mode is builder.BuildMode.FRESH
                              or (rebuilding and self._rewrite_boots()))
                             and bool(self.adf_row.path)
                             and (self._system_source() == "adf"
                                  or self._imported_needs_floppies())),
            adf_folder=self.adf_row.path,
            adf_version=self._selected_adf_version(),
            #  Everything the CD group decides, read from the widgets that
            #  decide it.  A control that is on screen and does not reach the
            #  card is worse than no control, so these are set here - where
            #  the card is actually written from - and not only where a quick
            #  setup is assembled.
            os_cd=(self.os_cd_row.path
                   if self._system_source() == "cd"
                   and (not rebuilding or self._rewrite_boots()) else ""),
            os_cd_release=self._os_cd_release(),
            os_cd_options=self._os_cd_chosen_options(),
            boingbag_archives=self._boingbag_archives(),
            boingbags=self._chosen_boingbags(),
            boingbag_emulator=self.boingbag_emulator.get_active(),
            accelerator=self._accelerator().value,
            accelerator_cpu=(self._accelerator_cpu().value
                             if self._accelerator_cpu() else ""),
            #  A rebuilt drive keeps the name it had: a System drive that comes
            #  back as "Workbench" breaks every assign and script naming it.
            amiga_volume_name=(
                self._rewrite_spec()[0].volume_name
                if rebuilding and self._rewrite_spec()
                else self.volume_row.get_text().strip() or "Workbench"),
            #  The software chosen on the Amiga page.  These only used to be
            #  set by the quick setup, so ticking a package and pressing Write
            #  from the pages themselves quietly built a card without it.
            package_keys=self._chosen_packages(),
            #  Only for software that is going on, and only where asked.
            with_media=[key for key, row in getattr(self, "media_rows",
                                                    {}).items()
                        if row.get_active()
                        and self.package_rows[key].get_active()],
            replace_older_software=self.replace_older_row.get_active(),
            #  Everything the user asked to be left out, from both lists.
            #  They read in opposite directions and mean the same thing: a
            #  drawer of the drive's own software switched *off* is one they
            #  do not want, and an older copy switched *on* is one they want
            #  replaced. Both go drawer, contents and icon.
            #
            #  These used to be two config fields, and the second was written
            #  and then read by nobody - so the whole "older copies" list did
            #  nothing at all, quietly, while looking as though it worked.
            #  A set, not a list: the three lists overlap - the drive's own
            #  VirusZ is both software the drive arrives with and an older
            #  copy of one that was chosen - and it appeared twice, which read
            #  as though the same drawer were being removed twice over.
            leave_out=sorted(set(
                [key for key, row in getattr(self, "arrives_rows", {}).items()
                 if not row.get_active()]
                + [drawer for drawer, row
                   in getattr(self, "older_rows", {}).items()
                   if row.get_active()]
                + [drawer for drawer, row
                   in getattr(self, "broken_rows", {}).items()
                   if row.get_active()]
                + [where for where, row
                   in getattr(self, "clutter_rows", {}).items()
                   if row.get_active()])),
            off_desktop=sorted(
                where for where, row in getattr(self, "desktop_rows", {}).items()
                if not row.get_active()),
            machine_key=self._machine().key,
            #  Which Raspberry Pi is on the board, and which of its USB
            #  sockets the Amiga was given. Both are read from the widgets
            #  that decide them, here, where the card is actually written
            #  from - not only where a quick setup is assembled.
            pi_model=self._pi().value,
            usb_port=usb_port.value if usb_port is not None else "",
            #  How much chip RAM the machine has, and what is going onto the
            #  boot partition beside the kernel. Read from the widgets that
            #  decide them, here, where the card is written from.
            chip_ram=self._chip_ram(),
            boot_addons=self._chosen_addons(),
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
            whdload_kickstarts=self.whdload_rom_row.path,
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

        if config.mode is builder.BuildMode.REWRITE:
            #  Asked whatever the target is: an image file loses that drive
            #  just as surely as a card does.
            drive = self._rewrite_drive()
            what = (f'{drive.name} ("{drive.volume}", {human_size(drive.size)})'
                    if drive is not None and drive.volume
                    else config.rewrite_drive)
            others = [d.name for d in getattr(self, "_rewrite_drives", [])
                      if d.name.upper() != config.rewrite_drive.upper()]
            body = (f"Everything on {what} will be erased, and the drive "
                    f"built again from what you chose.\n\nThe partition "
                    f"table, the boot partition"
                    + (f" and {', '.join(others)}" if others else "")
                    + f" are left exactly as they are.\n\n{config.target}")
            dialog = Adw.AlertDialog(heading=f"Rebuild {config.rewrite_drive}?",
                                     body=body)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("write", f"Erase and rebuild "
                                         f"{config.rewrite_drive}")
            dialog.set_response_appearance("write",
                                           Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect("response", self._on_confirm, config)
            dialog.present(self)
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

    def _on_progress_close(self, _window) -> bool:
        """Keep the log up while the build is still running.

        Closing it would leave an hour-long build with nowhere to report,
        and no way back to it. Cancel is the way to stop; the window closes
        by itself once there is nothing left to say.
        """
        if self.cancel_button.get_visible():
            self._toast("The build is still running - use Cancel to stop it")
            return True                       # refuse the close
        return False

    def _start(self, config: builder.BuildConfig) -> None:
        self.cancel_flag.clear()
        self.log_buffer.set_text("")
        self.progress_bar.set_fraction(0.0)
        self.step_label.set_text("Preparing…")
        self.progress_title.set_subtitle(config.target)
        self.cancel_button.set_visible(True)
        self.progress_back_button.set_visible(False)
        self.save_log_button.set_visible(False)
        self.progress_window.present()

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
            #  The helper runs as root, whose cache holds none of this
            #  user's archives, so it is told where to look.
            config = dataclasses.replace(
                config, cache_root=str(emu68.cache_dir()))
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

    def _where_the_card_goes(self) -> str:
        """What to do with the card that was just written."""
        if self._mode() is builder.BuildMode.REWRITE:
            return (f"{self._rewrite_drive_name()} has been rebuilt and "
                    f"nothing else on the card was touched. Eject it and put "
                    f"it back where it came from.")
        if self.amiga_only_row.get_active():
            #  The drive is not for the PiStorm's slot, but the machine may
            #  well have one: Emu68 then boots from the PiStorm's own card
            #  and finds this drive on the IDE port.
            behind = (" The PiStorm boots Emu68 from its own card, which this "
                      "one cannot replace."
                      if self._accelerator() is machines.Accelerator.PISTORM
                      else "")
            return ("Eject the card and put it on the Amiga's own IDE or "
                    "SCSI controller - it carries no boot partition, so a "
                    "PiStorm cannot start from it." + behind)
        if self.boot_only_row.get_active():
            return ("Eject the card and put it in your PiStorm. It carries "
                    "Emu68 and the Kickstart; the Amiga's drives are on your "
                    "own storage.")
        return "Eject the card and put it in your PiStorm."

    def _finished(self, success: bool, message: str) -> bool:
        self._remember_session()
        self.cancel_button.set_visible(False)
        self.progress_back_button.set_visible(True)
        self.save_log_button.set_visible(True)
        if success:
            self.step_label.set_text("Finished - the card is ready")
            self._set_fraction(1.0)
            #  Where the card goes depends on what was built.  An
            #  Amiga-drives-only card has no boot partition and no Emu68, so
            #  a PiStorm cannot boot it at all - it is for the machine's own
            #  IDE or SCSI controller. Saying otherwise sends somebody to fit
            #  a card that was never going to work in that slot.
            self._append_log("Done. " + self._where_the_card_goes())
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
            "boingbag_folder": self.boingbag_row.path,
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
            self.boingbag_row.set_path(state.get("boingbag_folder", ""))
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
        #  What the restored machine decides, so its own settings are only
        #  re-derived where they still match it: a value somebody set by hand
        #  is part of what is being restored.
        self._derived_boot = self._machine_boot_values()
        self._on_machine_changed()
        self._target_settled()
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
                        "saved by an older version and has been reset - check "
                        "the Drives step")
            return
        #  A card that is no longer plugged in cannot be the target; fall back
        #  to an image file rather than leaving nothing selected.
        if self.target_row.get_selected() == 0 and not self.device_list:
            self.target_row.set_selected(1)
            self._target_settled()
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
        """Forget the saved setup and put the window back as it opened.

        Deleting the file was all this did, so everything on screen stayed
        exactly as it was and only the *next* launch differed - which is not
        what anyone means by starting again. Clearing the widgets by hand was
        not it either: the storage layout stayed behind, because the relayout
        gives up when there is no target to lay anything out for.

        So the reset goes through apply(), the same method a loaded setup
        goes through, with a default configuration - which is every widget the
        configuration reaches, in one place, rather than a list to keep in
        step with the window.
        """
        try:
            jobs.session_file().unlink(missing_ok=True)
        except OSError as error:
            self._toast(f"Could not remove it: {error}")
            return
        was_ready, self._ready = self._ready, False
        try:
            #  What the configuration does not carry: the choosers, the
            #  machine, and the quick screen's own copies.
            for row in (self.quick_pimiga, self.quick_hdf, self.quick_donor,
                        self.file_row):
                row.set_path("")
            self.quick_primary.set_selected(PRIMARY_SOURCES.index("default"))
            self.quick_system_source.set_selected(0)
            self.quick_machine.set_selected(0)
            self.quick_display.set_selected(0)
            self.quick_workbench_screen.set_selected(0)
            self.quick_trapdoor.set_active(False)
            self.quick_system.set_text("1G")
            self.quick_work.set_active(True)
            self.target_row.set_selected(0)
            self.device_row.set_selected(0)
        finally:
            self._ready = was_ready
        self.apply(builder.BuildConfig(
            target="",
            package_keys=packages.suggested(machines.MACHINES[0],
                                            list(machines.Display)[0])),
                   derived=True)
        #  Whatever is lying about on this machine is found again, exactly as
        #  it is at startup: a Kickstart, the Workbench disks, a PFS3 handler.
        self._detect_material()
        self._derived_boot = None
        self._on_machine_changed()
        self._target_settled()
        self._relayout_partitions()
        self._set_customising(False)
        self._sync_visibility()
        self._update_summary()
        self._toast("Forgotten - starting again")

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
        """Open the About dialog and check there, so there is one check."""
        self._on_about(None)
        self.app_updater.check()

    def _on_about(self, _button) -> None:
        about = Adw.AboutDialog(
            application_name=APPLICATION_NAME,
            application_icon="drive-removable-media",
            developer_name="PiStorm Imager for Linux",
            version=__version__,
            comments=("Prepare an SD card for PiStorm and Emu68 on Linux: build a "
                      "new card, write a pre-built image such as PiMiga, or refresh "
                      "the boot partition of a card you already have."),
            license_type=Gtk.License.GPL_3_0,
        )
        controls = AppUpdateControls(self.app_updater)
        attach_to_about(about, controls)
        about.connect("closed", lambda _dialog: controls.detach())
        self.about_dialog = about
        about.present(self)

    # ------------------------------------------------------ applying config

    def apply(self, config: builder.BuildConfig, *,
              keep_partitions: bool = False, derived: bool = False) -> None:
        """Push a loaded BuildConfig back into the widgets."""
        was_ready, self._ready = self._ready, False
        for index, (_label, mode, _hint) in enumerate(MODES):
            if mode is config.mode:
                self.mode_row.set_selected(index)
        for index, variant in enumerate(emu68.VARIANTS):
            if variant.key == config.variant:
                self.variant_row.set_selected(index)
        self.install_emu_row.set_active(config.install_emu68)
        self.hdf_row.set_path(config.hdf_image)
        self.image_row.set_path(config.source_image or config.hdf_image)
        self.repair_row.set_active(config.repair_rdb)
        self.local_zip_row.set_path(config.emu68_archive)
        self.rom_row.set_path(config.kickstart_path)
        #  What is providing the processor, and the CD install.  A loaded
        #  setup that could not put these back would come up claiming a
        #  PiStorm whatever it was saved as.
        for index, accelerator in enumerate(machines.Accelerator):
            if accelerator.value == config.accelerator:
                self.quick_accelerator.set_selected(index)
        if config.accelerator_cpu:
            for index, cpu in enumerate(machines.Cpu):
                if cpu.value == config.accelerator_cpu:
                    self.quick_accelerator_cpu.set_selected(index)
        self._on_accelerator_changed()
        self.os_cd_row.set_path(config.os_cd)
        if config.os_cd_options is not None:
            for key, row in self.os_cd_options.items():
                row.set_active(key in config.os_cd_options)
        self.boingbag_emulator.set_active(config.boingbag_emulator)
        #  The community pack is a switch rather than a name in the list, so
        #  it is read back from whether the list carries it.
        if config.boingbags:
            self.boingbag_community.set_active(
                any(not boingbag.BAGS_BY_KEY[key].official
                    for key in config.boingbags
                    if key in boingbag.BAGS_BY_KEY))
        self._on_os_cd_chosen()
        self.rom_key_row.set_path(config.kickstart_key)
        self.whdload_rom_row.set_path(config.whdload_kickstarts)
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
        #  A loaded layout is somebody's own: recording it as derived told the
        #  relayout it was its to redraw, and four saved drives came back as
        #  the generic layout. Only a fresh start is the window's own, and the
        #  Drives step can always ask for the suggestion again.
        self._derived_partitions = (
            [row.spec() for row in self.partition_rows] if derived else [])

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
        self.noide_row.set_active(options.no_ide)
        standard = (options.video_standard
                    if options.video_standard in bootcfg.VIDEO_STANDARDS
                    else "")
        self.video_row.set_selected(bootcfg.VIDEO_STANDARDS.index(standard))
        self.jit_row.set_value(options.jit_cache_mb or 0)
        self.extra_row.set_text(options.extra_cmdline)

        self.replace_older_row.set_active(config.replace_older_software)
        self.patch_display_row.set_active(config.patch_display)
        self.expand_row.set_active(config.expand_to_fill)
        self.boot_only_row.set_active(config.boot_only)
        self.amiga_only_row.set_active(config.amiga_only)
        self._amiga_only_changed()
        self._boot_only_changed()
        for row in list(self.extra_rows):
            self.expand_group.remove(row)
        self.extra_rows.clear()
        for spec in config.extra_partitions:
            self._add_extra_partition(spec)
        if not self.extra_rows:
            self._add_extra_partition()
        self.target_row.set_selected(0 if config.target_is_device else 1)
        self.file_size_row.set_text(exact_size_text(config.image_size))
        #  A split build's drives, where they can be put back: an image file
        #  is a path, and a card is only the same card if it is plugged in.
        if config.drives_target and not config.drives_target_is_device:
            self.drives_kind_row.set_selected(1)
            self.drives_file_row.set_path(config.drives_target)
        self.drives_size_row.set_text(exact_size_text(config.drives_image_size))
        for key, row in getattr(self, "media_rows", {}).items():
            row.set_active(key in (config.with_media or []))
        if not config.target_is_device:
            self.file_row.set_path(config.target)
        #  Before the software, which is offered on the strength of it: a
        #  loaded setup whose Pi came back last would have had its USB
        #  choices refused while the list was being rebuilt.
        self._restore_pi(config)
        self._restore_chip_ram(config)
        self._restore_package_choices(config)
        self._restore_usb_port(config)
        self._restore_boot_addons(config)
        #  The list of Emu68 builds is fetched from GitHub in the background,
        #  so the one this setup was built against may not be offered yet.
        self._wanted_release = config.release_tag or ""
        self._populate_releases()
        #  After the releases, because the kernel row checks the chosen
        #  release before it will stay selected.
        keys = [k.key for k in emu68.KERNELS]
        self.kernel_row.set_selected(
            keys.index(config.kernel_key) + 1
            if config.kernel_key in keys else 0)
        self._on_kernel_changed()
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
        self._restore_pi(config)
        self._restore_chip_ram(config)
        self._restore_package_choices(config)
        self._restore_usb_port(config)
        self._restore_boot_addons(config)
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

    def _restore_pi(self, config: builder.BuildConfig) -> None:
        """Put the Raspberry Pi back before anything that depends on it."""
        self._refresh_pi_choices()
        choices = getattr(self, "_pi_choices", [])
        for index, pi in enumerate(choices):
            if pi.value == config.pi_model:
                self.quick_pi.set_selected(index)
                return

    def _restore_chip_ram(self, config: builder.BuildConfig) -> None:
        """Put the chip RAM back, before anything that is gated on it."""
        self._refresh_chip_ram_choices()
        choices = getattr(self, "_chip_ram_choices", [])
        if config.chip_ram in choices:
            self.quick_chip_ram.set_selected(choices.index(config.chip_ram))

    def _restore_boot_addons(self, config: builder.BuildConfig) -> None:
        """Put the boot-partition add-ons back, after what gates them.

        Which are on offer follows the machine, the Pi, the chip RAM and the
        Emu68 build, so this runs once all four are settled - a switch put
        back before them would be cleared by the refresh that followed.
        """
        self._refresh_boot_addons()
        wanted = set(config.boot_addons)
        was = getattr(self, "_settling_addons", False)
        self._settling_addons = True
        try:
            for key, row in getattr(self, "addon_rows", {}).items():
                if row.get_sensitive():
                    row.set_active(key in wanted)
        finally:
            self._settling_addons = was
        self._refresh_boot_addons()

    def _restore_usb_port(self, config: builder.BuildConfig) -> None:
        """Put the USB socket back, after the software that asks for one.

        The row only exists while something needs it, and its list is the
        sockets this Pi has - so this runs last, once both are settled.
        """
        self._refresh_usb()
        for index, port in enumerate(self._usb_ports()):
            if port.value == config.usb_port:
                self.usb_port_row.set_selected(index)
                return

    def _restore_package_choices(self, config: builder.BuildConfig) -> None:
        """Put the software choices back.

        gather() has always saved these; nothing ever put them back, so
        loading a setup returned a card with every tick cleared, however
        carefully the list had been chosen.
        """
        #  Which rows are on offer has to be worked out first: refreshing
        #  afterwards would clear anything it thought unusable, including
        #  choices that are perfectly usable. A row left insensitive is one
        #  the display makes essential, and it keeps the tick it was given.
        self._refresh_packages()
        wanted = set(config.package_keys)
        was = getattr(self, "_settling_packages", False)
        self._settling_packages = True
        try:
            for key, row in self.package_rows.items():
                if row.get_sensitive() or key in wanted:
                    row.set_active(key in wanted)
        finally:
            self._settling_packages = was
        self._tick_what_is_needed()
