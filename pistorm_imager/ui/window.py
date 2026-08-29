"""The main PiStorm Imager window."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core import (amigaos, bootcfg, builder, devices, emu68, hdfcheck, jobs,  # noqa: E402
                    kickstart, machines, prepare, presets, rdb)
from ..core.util import GIB, MIB, Progress, human_size, parse_size  # noqa: E402
from .widgets import FileRow, SaveRow, combo, show_full_value  # noqa: E402

SELECT_CARD = "Select a card…"

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


class PartitionRow(Adw.ExpanderRow):
    """Editor for one Amiga partition inside the RDB."""

    def __init__(self, spec: builder.AmigaPartitionSpec, on_remove, on_change):
        super().__init__()
        self._on_change = on_change
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

        for row in (self.name_row, self.volume_row, self.size_row, self.fs_row,
                    self.boot_row, self.priority_row, self.content_row):
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
        return dataclasses.replace(
            self._source,
            name=self.name_row.get_text().strip().upper() or "DH0",
            volume_name=self.volume_row.get_text().strip(),
            size=size,
            dostype=FILESYSTEMS[self.fs_row.get_selected()],
            bootable=self.boot_row.get_active(),
            boot_priority=int(self.priority_row.get_value()),
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
            self.quick_target, self.quick_device, self.hdmi_row,
            self.overclock_row, self.antenna_row, self.target_row,
            self.device_row, self.os_version_row,
        )
        self._ready = True
        self._mirror_target()
        self._on_machine_changed()
        self._detect_material()
        self._refresh_devices()
        self._restore_session()
        self.connect("close-request", self._on_close)
        self._load_releases_async()
        self._sync_visibility()

    # ------------------------------------------------------------ setup UI

    def _build_setup(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        menu = Gtk.MenuButton(icon_name="open-menu-symbolic")
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        for label, handler in (("Save settings…", self._on_save_settings),
                               ("Load settings…", self._on_load_settings),
                               ("Forget saved setup", self._on_forget_session),
                               ("Inspect the target", self._on_inspect),
                               ("About", self._on_about)):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.connect("clicked", handler)
            box.append(button)
        popover.set_child(box)
        menu.set_popover(popover)
        header.pack_end(menu)
        view.add_top_bar(header)

        self.stack.add_titled_with_icon(self._page_quick(), "quick", "Quick setup",
                                        "starred-symbolic")
        self.stack.add_titled_with_icon(self._page_source(), "source", "Source",
                                        "folder-download-symbolic")
        self.stack.add_titled_with_icon(self._page_amiga(), "amiga", "Amiga",
                                        "drive-harddisk-symbolic")
        self.stack.add_titled_with_icon(self._page_options(), "options", "Options",
                                        "preferences-system-symbolic")
        self.stack.add_titled_with_icon(self._page_target(), "target", "Target",
                                        "media-flash-symbolic")
        view.set_content(self.stack)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                         margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        self.summary = Gtk.Label(xalign=0.0, wrap=True, hexpand=True)
        self.summary.add_css_class("dim-label")
        bottom.append(self.summary)
        self.write_button = Gtk.Button(label="Write card")
        self.write_button.add_css_class("suggested-action")
        self.write_button.add_css_class("pill")
        self.write_button.connect("clicked", self._on_write)
        bottom.append(self.write_button)
        view.add_bottom_bar(bottom)
        return view

    def _page_quick(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()

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
                                   lambda *_a: self._quick_preview())
        group.add(self.quick_display)
        self.quick_trapdoor = Adw.SwitchRow(
            title="Trapdoor 512K fitted, use it as chip RAM",
            subtitle="A500 and A500+ only")
        group.add(self.quick_trapdoor)
        page.add(group)

        group = Adw.PreferencesGroup(
            title="Games, demos and WHDLoad",
            description="Point at a PiMiga installation to fill the card with "
                        "its games and demos, and to take WHDLoad from it. "
                        "Collections that need a chipset your machine does not "
                        "have are left out.")
        self.quick_pimiga = FileRow("PiMiga folder (optional)", folder=True,
                                    on_change=lambda _p: self._quick_preview())
        group.add(self.quick_pimiga)
        self.quick_pimiga_info = Adw.ActionRow(title="Content",
                                               subtitle="No folder selected")
        self.quick_pimiga_info.set_sensitive(False)
        group.add(self.quick_pimiga_info)
        self.quick_hdf = FileRow(
            "Amiga hard disk image (optional)",
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
            model=combo(["Choose for me",
                         "PiMiga's ready-made system",
                         "The hard disk image's own system",
                         "Install Workbench from my floppy images",
                         "Don't install one - partition only"]))
        self.quick_system_source.connect("notify::selected",
                                         lambda *_a: self._quick_preview())
        group.add(self.quick_system_source)
        hint = Adw.ActionRow(
            title="",
            subtitle="PiMiga's system is ready made but built around RTG; a "
                     "Workbench installed from floppies is small and uses "
                     "native screen modes.")
        hint.set_sensitive(False)
        group.add(hint)
        page.add(group)

        group = Adw.PreferencesGroup(title="Choices")
        self.quick_system = Adw.EntryRow(title="System drive size")
        self.quick_system.set_text("1G")
        self.quick_system.connect("changed", lambda _r: self._quick_preview())
        group.add(self.quick_system)
        self.quick_work = Adw.SwitchRow(
            title="Add a PFS3 work drive",
            subtitle="Takes the rest of the card; format it on the Amiga. Not "
                     "used when the layout comes from PiMiga or an image.")
        self.quick_work.set_active(True)
        self.quick_work.connect("notify::active", lambda *_a: self._quick_preview())
        group.add(self.quick_work)
        self.quick_donor = FileRow(
            "PFS3 handler",
            "Looking for one…", filters=HDF_FILTERS,
            on_change=lambda _p: self._quick_preview())
        group.add(self.quick_donor)
        page.add(group)

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
        self.quick_card_size = Adw.EntryRow(title="Card or image size")
        self.quick_card_size.set_text("64G")
        self.quick_card_size.connect("changed", lambda _r: self._mirror_target())
        group.add(self.quick_card_size)
        page.add(group)

        group = Adw.PreferencesGroup(
            title="What this will build",
            description="Choose the card or image file on the Target page first; "
                        "the sizes below follow from it.")
        self.quick_plan = Gtk.Label(xalign=0.0, wrap=True, selectable=True,
                                    margin_top=6, margin_bottom=6,
                                    margin_start=12, margin_end=12)
        self.quick_plan.add_css_class("dim-label")
        holder = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.quick_plan)
        box.add_css_class("card")
        group.add(box)
        apply_button = Gtk.Button(label="Apply this setup", halign=Gtk.Align.CENTER,
                                  margin_top=12)
        apply_button.add_css_class("suggested-action")
        apply_button.add_css_class("pill")
        apply_button.connect("clicked", self._on_apply_quick)
        group.add(apply_button)
        page.add(group)
        return page

    def _on_quick_hdf(self) -> None:
        path = self.quick_hdf.path
        if not path:
            self.quick_hdf_info.set_subtitle("No image selected")
            self._quick_preview()
            return
        scheme = presets.describe_image_scheme(path)
        system = presets.inspect_image_system(path)
        text = f"{scheme}. Contains {system.describe()}"
        if system.needs_floppies:
            text += ("  -  choose \u201cinstall Workbench from my floppy "
                     "images\u201d as well, or the card will not boot.")
        self.quick_hdf_info.set_subtitle(text)
        self._quick_preview()

    def _system_source(self) -> str:
        """The operating system source, with "choose for me" resolved."""
        requested = ["auto", "pimiga", "image", "adf", "none"][
            self.quick_system_source.get_selected()]
        if requested == "image":
            return "image" if self.quick_hdf.path else "adf"
        disks = (presets.pimiga_disks(self.quick_pimiga.path)
                 if self.quick_pimiga.path else None)
        try:
            size = parse_size(self.quick_card_size.get_text())
        except ValueError:
            size = 64 * GIB
        return presets.choose_system_source(self._display(), disks, size,
                                            requested)

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
        finally:
            self._mirroring = False
        self._sync_visibility()

    def _machine(self) -> machines.Machine:
        return machines.MACHINES[self.quick_machine.get_selected()]

    def _display(self) -> machines.Display:
        return list(machines.Display)[self.quick_display.get_selected()]

    def _on_machine_changed(self) -> None:
        if not self._ready:
            return
        machine = self._machine()
        self.quick_machine_hint.set_subtitle(
            f"{machine.board_label} - {machine.chipset.value} chipset "
            f"({machine.chipset.native_colours})")
        #  Keep the Source page's board in step with the model.
        for index, variant in enumerate(emu68.VARIANTS):
            if variant.key == machine.board:
                self.variant_row.set_selected(index)
        self.quick_trapdoor.set_visible(machine.trapdoor_ram)
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
        return presets.machine_setup(
            self._machine(), self._display(), base.target,
            base.target_is_device, size, detected,
            pimiga_folder=self.quick_pimiga.path,
            hdmi=(hdmi_choice[1], hdmi_choice[2]),
            system_size=system, trapdoor_to_chip=self.quick_trapdoor.get_active(),
            system_source=["auto", "pimiga", "image", "adf", "none"][
                self.quick_system_source.get_selected()],
            hdf_source=self.quick_hdf.path)

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
        try:
            config = self._quick_config()
        except Exception as error:  # noqa: BLE001 - no target chosen yet, usually
            self.quick_plan.set_text(str(error))
            return
        self.quick_plan.set_text(presets.describe_machine_setup(
            config, self._machine(), self._display(), detected))

    def _on_apply_quick(self, _button) -> None:
        try:
            config = self._quick_config()
        except Exception as error:  # noqa: BLE001
            self._toast(str(error))
            return
        self.apply(config)
        #  Remember it now, not only on a clean exit: this is the point at
        #  which the setup is worth keeping.
        self._remember_session()
        self.stack.set_visible_child_name("target")
        self._toast("Quick setup applied and remembered for next time")

    def _page_source(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()

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
            description="PiMiga, Emu68 Hatcher, or any .img backup. Compressed "
                        "images (.xz, .gz, .zip, .7z) are streamed straight to the "
                        "card, so no scratch space is needed.")
        self.image_row = FileRow("Image file", filters=IMAGE_FILTERS,
                                 on_change=lambda _p: self._on_image_chosen())
        self.image_group.add(self.image_row)
        self.image_info = Adw.ActionRow(title="Image details", subtitle="No image selected")
        self.image_info.set_sensitive(False)
        self.image_group.add(self.image_info)
        page.add(self.image_group)

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

        group = Adw.PreferencesGroup(
            title="Kickstart ROM",
            description="Emu68 maps a Kickstart from the boot partition. An A1200 "
                        "(AGA) ROM is expected. Cloanto-encrypted ROMs are decrypted "
                        "automatically when rom.key sits beside them.")
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

        self.partition_group = Adw.PreferencesGroup(
            title="Amiga partitions",
            description="Written as a Rigid Disk Block inside the 0x76 partition. "
                        "Format them from HDToolBox on the Amiga.")
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Add a partition")
        add.add_css_class("flat")
        add.connect("clicked", lambda _b: self._add_partition())
        self.partition_group.set_header_suffix(add)
        page.add(self.partition_group)
        self.partition_rows: list[PartitionRow] = []
        self._add_partition(builder.AmigaPartitionSpec("DH0", None, "PFS3", True, 0))

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
            subtitle="Faster, but breaks many floppy-loaded games and demos")
        group.add(self.vbr_row)
        self.slowdown_row = Adw.SwitchRow(
            title="Chip RAM slowdown",
            subtitle="Improves compatibility with software that busy-waits")
        group.add(self.slowdown_row)
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
        self.device_row.connect("notify::selected", lambda *_a: self._update_summary())
        self.device_group.add(self.device_row)
        page.add(self.device_group)

        self.file_group = Adw.PreferencesGroup(
            title="Image file",
            description="A sparse .img file you can write to a card later, or use "
                        "with an emulator.")
        self.file_row = SaveRow("Save image as", filters=IMAGE_FILTERS,
                                on_change=lambda _p: self._update_summary())
        self.file_group.add(self.file_row)
        self.file_size_row = Adw.EntryRow(title="Image size (e.g. 8G)")
        self.file_size_row.set_text("8G")
        self.file_size_row.connect("changed", lambda _r: self._update_summary())
        self.file_group.add(self.file_size_row)
        page.add(self.file_group)

        self.boot_group = Adw.PreferencesGroup(
            title="Boot partition",
            description="Holds Emu68, the Raspberry Pi firmware and your Kickstart.")
        self.boot_size_row = Adw.EntryRow(title="Size")
        self.boot_size_row.set_text("256M")
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

    def _making_hdf(self) -> bool:
        """True when the output is a bare Amiga drive rather than a card."""
        return self.target_row.get_selected() == 2

    def _sync_visibility(self) -> None:
        if not self._ready:
            return
        mode = self._mode()
        making_hdf = self._making_hdf()
        self.mode_hint.set_subtitle(MODES[self.mode_row.get_selected()][2])
        self.image_group.set_visible(mode is builder.BuildMode.IMAGE)
        self.hdf_group.set_visible(mode is builder.BuildMode.HDF)
        self.partition_group.set_visible(mode is builder.BuildMode.FRESH)
        self.os_group.set_visible(mode is builder.BuildMode.FRESH)
        installing = (not self.quick_hdf.path
                      and self._system_source() == "adf")
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
        self.file_size_row.set_title("Drive size (e.g. 2G)" if making_hdf
                                     else "Image size (e.g. 8G)")
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
        row = PartitionRow(spec, self._remove_partition, self._update_summary)
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
        row = PartitionRow(spec, self._remove_extra_partition, self._update_summary)
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

    def _on_image_chosen(self) -> None:
        from ..core import imgsrc
        if not self.image_row.path:
            self.image_info.set_subtitle("No image selected")
            self._update_summary()
            return
        try:
            source = imgsrc.inspect(self.image_row.path)
            self.image_info.set_subtitle(source.description)
        except Exception as error:  # noqa: BLE001
            self.image_info.set_subtitle(f"Cannot read this file: {error}")
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
        try:
            config = self.gather()
        except Exception as error:  # noqa: BLE001 - partial input while typing
            self.summary.set_text(str(error))
            self.write_button.set_sensitive(False)
            return
        problems = config.validate()
        if problems:
            self.summary.set_text(problems[0])
            self.write_button.set_sensitive(False)
            return
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
        self.summary.set_text(f"{what} → {target}")
        self.write_button.set_sensitive(True)
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
            swap_df0_with_df1=self.swapdf_row.get_active(),
            sd_unit0_rw=self.unit0_row.get_active(),
            extra_cmdline=self.extra_row.get_text().strip(),
        )

        release_tag = ""
        choices = getattr(self, "_release_choices", [])
        if choices:
            index = min(self.release_row.get_selected(), len(choices) - 1)
            release_tag = choices[index].tag

        try:
            image_size = parse_size(self.file_size_row.get_text())
        except ValueError:
            image_size = 8 * GIB
        try:
            boot_size = parse_size(self.boot_size_row.get_text())
        except ValueError:
            boot_size = 256 * MIB

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
        """The choices a BuildConfig cannot express, so they can be restored."""
        return {
            "machine": self._machine().key,
            "display": self._display().name,
            "system_source": self.quick_system_source.get_selected(),
            "pimiga_folder": self.quick_pimiga.path,
            "hdf_source": self.quick_hdf.path,
            "pfs3_handler": self.quick_donor.path,
            "kickstart": self.rom_row.path,
            "kickstart_key": self.rom_key_row.path,
            "adf_folder": self.adf_row.path,
            "trapdoor": self.quick_trapdoor.get_active(),
            "card_size": self.quick_card_size.get_text(),
            "system_size": self.quick_system.get_text(),
            "boot_size": self.boot_size_row.get_text(),
            "target_kind": self.quick_target.get_selected(),
            "image_path": self.quick_file.path,
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
            self.quick_system_source.set_selected(
                int(state.get("system_source", 0)))
            self.quick_pimiga.set_path(state.get("pimiga_folder", ""))
            self.quick_hdf.set_path(state.get("hdf_source", ""))
            self.quick_donor.set_path(state.get("pfs3_handler", ""))
            self.rom_row.set_path(state.get("kickstart", ""))
            self.rom_key_row.set_path(state.get("kickstart_key", ""))
            self.adf_row.set_path(state.get("adf_folder", ""))
            self.quick_trapdoor.set_active(bool(state.get("trapdoor")))
            for row, key in ((self.quick_card_size, "card_size"),
                             (self.quick_system, "system_size"),
                             (self.boot_size_row, "boot_size")):
                if state.get(key):
                    row.set_text(str(state[key]))
            #  Default to whatever the build itself implies, rather than to
            #  "SD card": restoring a card that is not plugged in would leave
            #  the whole page in an error state.
            if "target_kind" in state:
                self.quick_target.set_selected(int(state["target_kind"]))
            if state.get("image_path"):
                self.quick_file.set_path(state["image_path"])
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
            config, state = jobs.load_session()
        except Exception as error:  # noqa: BLE001 - a stale file is not fatal
            self._append_log(f"Could not restore the last session: {error}")
            return
        try:
            self.apply(config)
            self.apply_interface_state(state)
        except Exception as error:  # noqa: BLE001
            self._toast(f"Could not restore the last session: {error}")
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
                config, state = jobs.load_session(file.get_path())
                self.apply(config)
                self.apply_interface_state(state)
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

    def _on_about(self, _button) -> None:
        about = Adw.AboutDialog(
            application_name="PiStorm Imager",
            application_icon="drive-removable-media",
            developer_name="PiStorm Imager for Linux",
            version="0.1.0",
            comments=("Prepare an SD card for PiStorm and Emu68 on Linux: build a "
                      "new card, write a pre-built image such as PiMiga, or refresh "
                      "the boot partition of a card you already have."),
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self)

    # ------------------------------------------------------ applying config

    def apply(self, config: builder.BuildConfig) -> None:
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
        #  The operating system combo is the only place this is recorded now.
        self.quick_system_source.set_selected(3 if config.install_amigaos else 4)
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
        self.swapdf_row.set_active(options.swap_df0_with_df1)
        self.unit0_row.set_active(options.sd_unit0_rw)
        self.extra_row.set_text(options.extra_cmdline)

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
        self.quick_card_size.set_text(
            human_size(config.image_size).replace(" GiB", "G")
            .replace(" MiB", "M").replace(".00", ""))
        if not config.target_is_device:
            self.file_row.set_path(config.target)
        self._ready = was_ready
        self._sync_visibility()
