"""Construct the window for real and exercise the settings round trip.

This runs a genuine GTK application (it needs a display), builds every page,
flips through the task modes and checks that a configuration survives being
written into the widgets and read back out.
"""
import os
import sys
import tempfile as _tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#  Applying a quick setup saves the session, and the real one belongs to
#  whoever is running the test.  Point the config directory somewhere of our
#  own before anything can read it.
os.environ["XDG_CONFIG_HOME"] = _tempfile.mkdtemp(prefix="pistorm-gui-config-")

from pistorm_imager.app import ImagerApplication  # noqa: E402  (also scrubs snap GTK env)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from pistorm_imager.core import bootcfg, builder  # noqa: E402
from pistorm_imager.ui.window import MODES  # noqa: E402

import tempfile  # noqa: E402

#  Self-contained stand-ins so the test never depends on files left behind by
#  an earlier run.
SCRATCH = Path(tempfile.mkdtemp(prefix="pistorm-gui-test-"))
SOURCE_IMAGE = SCRATCH / "source.img"
SOURCE_IMAGE.write_bytes(b"\0" * 4096)
#  A real Rigid Disk Block with two named drives, so the drive chooser has
#  something to list.  An empty file would leave it with nothing to offer and
#  the check below would prove nothing.
HDF_IMAGE = SCRATCH / "disk.hdf"
#  The real Workbench disks, so the ADF-install path is exercised as
#  the user actually drives it.
ADF_FOLDER = Path(__file__).resolve().parent.parent / "samples" / "workbench"


def _make_test_hdf() -> None:
    from pistorm_imager.core import rdb  # noqa: PLC0415
    geometry = rdb.Geometry()
    size = 64 * 1024 * 1024
    table = rdb.Rdb(
        geometry=geometry,
        partitions=[
            rdb.Partition("DH0", 1, 40, rdb.DOSTYPE_FFS_INTL, bootable=True),
            rdb.Partition("DH1", 41, 80, rdb.DOSTYPE_FFS_INTL),
        ],
        filesystems=[],
        cylinders=(size // 512) // geometry.cyl_blocks)
    with open(HDF_IMAGE, "wb") as handle:
        handle.truncate(size)
        table.write(handle, 0)


_make_test_hdf()

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


def on_activate(app: ImagerApplication) -> None:
    try:
        window = app.window
        check(window is not None, "window constructed")

        for index in range(len(MODES)):
            window.mode_row.set_selected(index)
            window._sync_visibility()
        check(True, f"all {len(MODES)} task modes render without error")

        window.mode_row.set_selected(0)
        window._sync_visibility()
        before = len(window.partition_rows)
        window._add_partition()
        window._add_partition()
        check(len(window.partition_rows) == before + 2, "partitions can be added")
        window._remove_partition(window.partition_rows[-1])
        check(len(window.partition_rows) == before + 1, "partitions can be removed")

        config = builder.BuildConfig(
            mode=builder.BuildMode.IMAGE,
            target="/tmp/pistorm-gui-test.img",
            target_is_device=False,
            source_image=str(SOURCE_IMAGE),
            hdf_image=str(HDF_IMAGE),
            variant="pistorm",
            install_emu68=True,
            boot_size=192 * 1024 * 1024,
            amiga_partitions=[
                builder.AmigaPartitionSpec("DH0", 2 * 1024**3, "PFS3", True, 0),
                builder.AmigaPartitionSpec("DH1", None, "FFS-INTL", False, -128),
            ],
            boot_options=bootcfg.BootOptions(
                hdmi_group=2, hdmi_mode=82, overclock=True,
                cm4_external_antenna=False, vc4_mem=64, vbr_move=True,
                chip_slowdown=True, extra_cmdline="sd.verbose=1"),
            wifi_ssid="Amiga", wifi_password="secret", wifi_country="IE",
            expand_to_fill=True,
            extra_partitions=[
                builder.AmigaPartitionSpec("DH4", 512 * 1024**2, "PFS3", False, -128),
                builder.AmigaPartitionSpec("DH5", None, "FFS-INTL", False, -128)],
        )
        window.apply(config)
        window.file_row.set_path(config.target)
        result = window.gather()

        check(result.mode is config.mode, "task mode survives a round trip")
        check(result.variant == config.variant, "board choice survives a round trip")
        check(result.source_image == config.source_image, "source image survives")
        check(result.hdf_image == config.hdf_image, "hard disk image survives")
        check(result.boot_options.hdmi_mode == 82, "HDMI mode survives")
        check(result.boot_options.overclock is True, "overclock survives")
        check(result.boot_options.cm4_external_antenna is False, "antenna survives")
        check(result.boot_options.vc4_mem == 64, "vc4.mem survives")
        check(result.boot_options.vbr_move and result.boot_options.chip_slowdown,
              "cmdline switches survive")
        check(result.boot_options.extra_cmdline == "sd.verbose=1", "extra cmdline survives")
        check(result.wifi_ssid == "Amiga" and result.wifi_password == "secret",
              "WiFi settings survive")
        check([p.name for p in result.amiga_partitions] == ["DH0", "DH1"],
              "partition names survive")
        check(result.amiga_partitions[0].size == 2 * 1024**3,
              f"partition size survives (got {result.amiga_partitions[0].size})")
        check(result.amiga_partitions[1].size is None,
              "'rest of the card' partition survives")
        check(result.amiga_partitions[1].dostype == "FFS-INTL", "file system survives")
        check(result.expand_to_fill
              and [p.name for p in result.extra_partitions] == ["DH4", "DH5"]
              and result.extra_partitions[0].size == 512 * 1024**2,
              f"extra partitions survive ({[p.name for p in result.extra_partitions]})")
        check(result.validate() == [], f"gathered config is valid: {result.validate()}")

        window.mode_row.set_selected(0)
        window._sync_visibility()
        check(window.gather().mode is builder.BuildMode.FRESH, "mode switch takes effect")

        #  The PiMiga summary must update even before a target is chosen: it
        #  used to sit after an early return and so never ran.
        window.target_row.set_selected(0)          # SD card, none selected
        window.device_row.set_selected(0)          # the "Select a card" row
        pimiga = SCRATCH / "pimiga" / "disks"
        for drive in ("System", "Games", "Demos", "Work"):
            (pimiga / drive).mkdir(parents=True, exist_ok=True)
        before = window.quick_pimiga_info.get_subtitle()
        window.quick_pimiga.set_path(str(SCRATCH / "pimiga"))
        after = window.quick_pimiga_info.get_subtitle()
        check(after != before and "Found" in after,
              f"PiMiga content is described without a target chosen ({after!r})")
        window.quick_pimiga.set_path("")
        check("No folder selected" in window.quick_pimiga_info.get_subtitle(),
              "clearing the PiMiga folder resets the description")
        #  Put the target back: later checks gather a full configuration.
        window.target_row.set_selected(1)
        window.file_row.set_path(str(SCRATCH / "restored.img"))

        #  Saved settings must carry the hardware choices, not just the build:
        #  a BuildConfig alone cannot say which Amiga or which display.
        window.quick_machine.set_selected(
            next(i for i, m in enumerate(__import__(
                "pistorm_imager.core.machines", fromlist=["x"]).MACHINES)
                if m.key == "a600"))
        window.quick_trapdoor.set_active(True)
        window.quick_card_size.set_text("32G")
        window.quick_pimiga.set_path(str(SCRATCH / "pimiga"))
        saved = window.interface_state()
        check(saved["machine"] == "a600" and saved["card_size"] == "32G",
              "interface state captures the hardware choices")

        window.quick_machine.set_selected(0)
        window.quick_card_size.set_text("8G")
        window.quick_pimiga.set_path("")
        window.apply_interface_state(saved)
        restored = window.interface_state()
        check(restored["machine"] == "a600", "machine is restored")
        check(restored["card_size"] == "32G", "sizes are restored")
        check(restored["pimiga_folder"] == saved["pimiga_folder"],
              "folders are restored")

        #  A source defines the partition layout, so changing it must redraw
        #  it: the rows are what a build reads, and leaving them behind would
        #  copy from a place the user had just cleared.
        window.quick_system_source.set_selected(0)          # "Choose for me"
        window.target_row.set_selected(1)
        window.quick_target.set_selected(1)
        window.quick_file.set_path(str(SCRATCH / "layout.img"))
        pimiga_root = SCRATCH / "pimiga"
        for drive in ("System", "Games", "Demos", "Work"):
            (pimiga_root / "disks" / drive).mkdir(parents=True, exist_ok=True)
        window.quick_pimiga.set_path(str(pimiga_root))
        with_source = window.gather().amiga_partitions
        check(len(with_source) == 4 and all(p.content_folder for p in with_source[1:]),
              f"a PiMiga source lays out its drives ({[p.name for p in with_source]})")
        #  Every choice that shapes the layout must redraw it, or the page and
        #  the build disagree about what is being made.
        window.quick_card_size.set_text("16GB")
        smaller = window.gather().amiga_partitions
        check(sum(p.size or 0 for p in smaller) < sum(p.size or 0 for p in with_source),
              "changing the card size resizes the partitions")

        window.quick_work.set_active(False)
        window.quick_pimiga.set_path("")
        without = window.gather().amiga_partitions
        check(not any(p.content_folder for p in without),
              "clearing the source clears the partitions that came from it")
        check(len(without) == 1,
              f"turning off the work drive leaves one partition ({len(without)})")
        window.quick_work.set_active(True)
        check(len(window.gather().amiga_partitions) == 2,
              "turning it back on restores it")

        #  A layout the user has edited by hand must survive the next change.
        window.partition_rows[0].name_row.set_text("DX0")
        window.quick_card_size.set_text("8GB")
        kept = [p.name for p in window.gather().amiga_partitions]
        check("DX0" in kept, f"a hand-edited partition is not overwritten ({kept})")

        #  Editing a partition must not discard what the editor does not show:
        #  a drive whose contents come from PiMiga would otherwise be built
        #  empty, with nothing to say so.
        rich = builder.AmigaPartitionSpec(
            "DH2", 2 * 1024**3, "PFS3", False, -128,
            content_folder="/somewhere/Games", volume_name="Games",
            exclude=["WHDLOAD/AGA"], overlays=[("/a/b", "C")])
        for row in list(window.partition_rows):
            window.partition_group.remove(row)
        window.partition_rows.clear()
        window._add_partition(rich)
        back = window.partition_rows[0].spec()
        check(back.content_folder == "/somewhere/Games",
              "editing a partition keeps where its contents come from")
        check(back.volume_name == "Games", "volume name survives")
        check(back.exclude == ["WHDLOAD/AGA"], "exclusions survive")
        check(back.overlays == [("/a/b", "C")], "overlays survive")
        window.partition_rows[0].volume_row.set_text("Spiele")
        check(window.partition_rows[0].spec().volume_name == "Spiele",
              "volume name is editable")
        check(window.partition_rows[0].spec().content_folder == "/somewhere/Games",
              "editing the volume name does not lose the contents")
        #  Leave a partition the later checks can actually build.
        for row in list(window.partition_rows):
            window.partition_group.remove(row)
        window.partition_rows.clear()
        window._add_partition(
            builder.AmigaPartitionSpec("DH0", None, "PFS3", True, 0))

        #  The boot partition takes its space from the Amiga drives, so typing
        #  a size there has to reach the layout - it used to be ignored by the
        #  quick setup and then overwritten by it.
        window.quick_system_source.set_selected(0)
        window.quick_card_size.set_text("8GB")
        window.boot_size_row.set_text("512M")
        check(window.gather().boot_size == 512 * 1024 * 1024,
              f"the boot size is used ({window.gather().boot_size})")
        check(window._quick_config().boot_size == 512 * 1024 * 1024,
              "the quick setup uses it too, rather than its own default")
        window.boot_size_row.set_text("256M")

        #  The primary source is one choice of three, and the two it is not
        #  must not be left holding paths that would still reach the build.
        window.apply_interface_state({"system_source": "adf",
                                      "pimiga_folder": "", "hdf_source": ""})
        check(window._primary() == "default" and window._system_source() == "adf",
              "Default installs Workbench from floppies")
        check(not window.quick_pimiga.get_visible()
              and not window.quick_hdf.get_visible(),
              "Default shows neither source chooser")
        window.quick_primary.set_selected(1)
        check(window.quick_pimiga.get_visible()
              and not window.quick_system_source.get_visible(),
              "PiMiga replaces the operating system question")
        check(window._system_source() == "none",
              "PiMiga with no folder yet installs nothing")
        window.quick_pimiga.set_path(str(SCRATCH / "pimiga"))
        check(window._system_source() == "pimiga", "a PiMiga folder is the system")
        window.quick_primary.set_selected(2)
        check(window.quick_pimiga.path == "" and window.quick_hdf.get_visible(),
              "choosing an image drops the PiMiga folder")
        window.quick_hdf.set_path(str(HDF_IMAGE))
        window.quick_primary.set_selected(0)
        check(window.quick_hdf.path == "" and window.quick_pimiga.path == "",
              "going back to Default drops both")
        state = window.interface_state()
        check(state["primary_source"] == "default",
              "the primary source is saved with the session")
        window.apply_interface_state({"system_source": "image",
                                      "hdf_source": str(HDF_IMAGE)})
        check(window._primary() == "image",
              "a session saved before the split still picks the right source")

        #  A tree can hold more than this machine can run - the AGA games on
        #  an OCS A500 - so what to leave out is editable per partition.
        from pistorm_imager.ui.window import PartitionRow as _PR  # noqa: PLC0415
        from pistorm_imager.core import machines as _m  # noqa: PLC0415

        #  A tree divided into categories, some of which an A500 cannot run.
        tree = SCRATCH / "GamesTree" / "WHDLOAD"
        for name in ("AGA", "CD32", "OCS", "Cinemaware"):
            (tree / name / "Something").mkdir(parents=True, exist_ok=True)

        row = _PR(builder.AmigaPartitionSpec("DH1", None, "PFS3",
                                             volume_name="Games"),
                  on_remove=lambda _r: None, on_change=None,
                  machine=lambda: _m.MACHINES_BY_KEY["a500"])
        check(row.spec().exclude == [],
              "a partition with no content leaves nothing out")
        row.hdf_row.set_path(str(SCRATCH / "GamesTree"))
        found = sorted(row._category_rows)
        check(len(found) == 4, f"the tree's categories are listed ({found})")
        check(sorted(row.spec().exclude) == ["WHDLOAD/AGA", "WHDLOAD/CD32"],
              f"an A500 leaves out what it cannot run ({row.spec().exclude})")
        row._category_rows["WHDLOAD/AGA"].set_active(False)
        check(row.spec().exclude == ["WHDLOAD/CD32"],
              "the default can be overridden per category")

        aga = _PR(builder.AmigaPartitionSpec("DH2", None, "PFS3"),
                  on_remove=lambda _r: None, on_change=None,
                  machine=lambda: next(m for m in _m.MACHINES if m.aga))
        aga.hdf_row.set_path(str(SCRATCH / "GamesTree"))
        check(aga.spec().exclude == [],
              f"an AGA machine leaves nothing out ({aga.spec().exclude})")

        kept = _PR(builder.AmigaPartitionSpec("DH3", None, "PFS3",
                                              exclude=["WHDLOAD/Nowhere"]),
                   on_remove=lambda _r: None, on_change=None,
                   machine=lambda: _m.MACHINES_BY_KEY["a500"])
        kept.hdf_row.set_path(str(SCRATCH / "GamesTree"))
        check("WHDLOAD/Nowhere" in kept.spec().exclude,
              "an exclusion the tree cannot explain is kept, not dropped")

        #  Applying the quick setup must not throw away partitions that have
        #  been arranged by hand.
        window._derived_partitions = [r.spec() for r in window.partition_rows]
        check(window._hand_edited_partitions() is None,
              "an untouched layout is not treated as hand-edited")
        window.partition_rows[0].volume_row.set_text("MyOwnName")
        edited = window._hand_edited_partitions()
        check(edited is not None and edited[0].volume_name == "MyOwnName",
              "an edited layout is noticed and would be kept")

        #  The quick start is a choice of three things to do, and is the
        #  whole window until "Customise" is chosen.
        window._set_customising(False)
        visible = {name for name in ("quick", "source", "storage", "amiga",
                                     "options", "target")
                   if window.stack.get_page(
                       window.stack.get_child_by_name(name)).get_visible()}
        check(visible == {"quick"},
              f"the quick start is the only page to begin with ({visible})")
        check(not window.back_to_quick.get_visible(),
              "and there is nothing to go back to")

        window._set_customising(True)
        visible = {name for name in ("quick", "source", "storage", "amiga",
                                     "options", "target")
                   if window.stack.get_page(
                       window.stack.get_child_by_name(name)).get_visible()}
        check("quick" not in visible and "storage" in visible,
              f"customising shows the workflow and hides the quick start ({visible})")
        check(window.back_to_quick.get_visible(),
              "and offers a way back to it")
        window._set_customising(False)
        check(window.stack.get_visible_child_name() == "quick",
              "going back returns to the quick start")

        #  Settings belong on the page they are about.
        window._set_customising(True)
        for group, page_name in ((window.group_hardware, "amiga"),
                                 (window.group_primary, "source"),
                                 (window.group_sizes, "storage")):
            holder = group.get_ancestor(Adw.PreferencesPage)
            found = holder is window.stack.get_child_by_name(page_name)
            check(found, f"a group lives on the {page_name} page")

        #  Editing storage has to show up in the plan the user reads before
        #  pressing Write; it used to describe the quick settings alone.
        window.file_row.set_path(str(SCRATCH / "plan.img"))
        window._update_summary()
        before = window.quick_plan.get_text()
        window.partition_rows[0].volume_row.set_text("PlanCheck")
        window._update_summary()
        after = window.quick_plan.get_text()
        check(before != after and "PlanCheck" in after,
              "a storage change shows up in the plan")

        #  Software ticked on the Amiga page has to reach the build.  It used
        #  to be carried only by the quick setup, so ticking a package and
        #  pressing Write from the pages themselves built a card without it.
        window.mode_row.set_selected(0)                   # a fresh card
        window.quick_system_source.set_selected(0)        # from floppies
        window.adf_row.set_path(str(ADF_FOLDER))
        window._refresh_packages()
        for key in ("whdload", "fblit"):
            if key in window.package_rows:
                window.package_rows[key].set_active(True)
        chosen = window._chosen_packages()
        config = window.gather()
        check(set(config.package_keys) == set(chosen) and bool(chosen),
              f"chosen software reaches the build ({chosen})")
        check(config.package_chipset and config.package_display,
              "the machine and display travel with it")

        #  A folder is genuinely hard to pick in the GTK dialog - you have to
        #  highlight it from its parent, and once inside it nothing is
        #  selected and Open greys out - so a path can be typed or pasted.
        window.quick_pimiga.set_text("/mnt/pimiga/home/pi/pimiga")
        check(window.quick_pimiga.path == "/mnt/pimiga/home/pi/pimiga",
              "a typed path is taken as the choice")
        window.quick_pimiga.set_text("'/mnt/pimiga/home/pi/pimiga'")
        check(window.quick_pimiga.path == "/mnt/pimiga/home/pi/pimiga",
              "a quoted path pastes cleanly")
        window.quick_pimiga.set_text("file:///mnt/pimiga/home/pi/pimiga")
        check(window.quick_pimiga.path == "/mnt/pimiga/home/pi/pimiga",
              "a file:// URI from a file manager pastes cleanly")
        window.quick_pimiga.set_path("")

        #  A partition can take its contents from an image of its own, so a
        #  drive out of an .hdf can be added to a PiMiga card rather than
        #  replacing it.
        from pistorm_imager.ui.window import PartitionRow  # noqa: PLC0415
        extra = PartitionRow(
            builder.AmigaPartitionSpec("DH4", None, "PFS\\3",
                                       content_folder="/somewhere/Games"),
            on_remove=lambda _r: None, on_change=None)
        #  Either a folder or an image is a valid answer for a partition's
        #  contents - PiMiga's Games and Demos drives are folders - so the row
        #  offers both and works out which was chosen.
        folder = SCRATCH / "GamesTree"
        folder.mkdir(exist_ok=True)
        extra.hdf_row.set_path(str(folder))
        spec = extra.spec()
        check(spec.content_folder == str(folder) and spec.content_hdf == "",
              "a folder fills the partition from a tree of files")

        extra.hdf_row.set_path(str(HDF_IMAGE))
        listed = [extra.hdf_part_row.get_model().get_string(i)
                  for i in range(extra.hdf_part_row.get_model().get_n_items())]
        check(any("DH0" in text for text in listed)
              and any("DH1" in text for text in listed),
              f"the drives in the image are offered by name ({len(listed)} entries)")
        extra.choose_drive("dh1")
        check(extra.spec().content_hdf == str(HDF_IMAGE)
              and extra.spec().content_hdf_partition == "DH1"
              and extra.spec().content_folder == "",
              "an image replaces the folder source rather than joining it")

        #  The display choice lives on the Quick setup page but decides what
        #  happens to a copied system's graphics setup.  gather() is what a
        #  build actually reads, so it has to carry it: without this, applying
        #  a quick setup and pressing Write removed the emulator's RTG driver
        #  instead of replacing it, however the display was set.
        from pistorm_imager.core import machines  # noqa: PLC0415
        displays = list(machines.Display)
        window.quick_display.set_selected(displays.index(machines.Display.RTG_HDMI))
        window._sync_visibility()
        check(window.gather().rtg_display,
              "an RTG display reaches the build")
        check(not window.quick_workbench_screen.get_visible(),
              "one output leaves no screen to choose between")
        window.quick_display.set_selected(displays.index(machines.Display.BOTH))
        window._sync_visibility()
        check(window.quick_workbench_screen.get_visible(),
              "two outputs offer a choice of where Workbench opens")
        both = window.gather()
        check(both.rtg_display and both.native_display,
              "both outputs reach the build")
        check(both.workbench_on_rtg, "Workbench defaults to the RTG screen")
        window.quick_workbench_screen.set_selected(1)
        check(not window.gather().workbench_on_rtg,
              "choosing the Amiga's own output reaches the build")
        state = window.interface_state()
        check(state["workbench_screen"] == "native",
              "where Workbench opens is saved with the session")
        window.quick_display.set_selected(displays.index(machines.Display.NATIVE))
        window._sync_visibility()
        check(not window.gather().rtg_display
              and not window.gather().workbench_on_rtg,
              "a native-only display cannot put Workbench on RTG")

        #  The Quick setup page builds a whole configuration from the machine
        #  and the card.  Everything chosen on another page - the WiFi network,
        #  the volume name, the boot switches - used to come back at its
        #  default the moment Apply was pressed, and the session was then saved
        #  in that state, so those settings could not be kept at all.
        window.mode_row.set_selected(0)
        window._sync_visibility()
        window.quick_primary.set_selected(0)
        window.target_row.set_selected(1)
        window.quick_target.set_selected(1)
        window.quick_file.set_path(str(SCRATCH / "quick.img"))
        window.file_row.set_path(str(SCRATCH / "quick.img"))
        window.quick_card_size.set_text("8G")
        window.ssid_row.set_text("Amiga")
        window.psk_row.set_text("hunter2")
        window.country_row.set_text("IE")
        window.volume_row.set_text("PiMiga")
        window.overclock_row.set_selected(1)
        window.swapdf_row.set_active(True)
        window.unit0_row.set_active(True)
        window.extra_row.set_text("sd.verbose=1")
        window.install_emu_row.set_active(False)

        quick = window._quick_config()
        check(quick.wifi_ssid == "Amiga" and quick.wifi_password == "hunter2"
              and quick.wifi_country == "IE",
              f"the quick setup keeps the WiFi settings ({quick.wifi_ssid!r})")
        check(quick.amiga_volume_name == "PiMiga",
              f"it keeps the volume name ({quick.amiga_volume_name!r})")
        check(quick.boot_options.overclock is True
              and quick.boot_options.swap_df0_with_df1
              and quick.boot_options.sd_unit0_rw,
              "it keeps the boot switches only a person can set")
        check(not quick.install_emu68, "it keeps Emu68 installation turned off")

        window._on_apply_quick(None)
        after = window.gather()
        check(after.wifi_ssid == "Amiga" and after.wifi_password == "hunter2"
              and after.wifi_country == "IE",
              f"applying it leaves the WiFi settings on the page ({after.wifi_ssid!r})")
        check(after.amiga_volume_name == "PiMiga",
              f"applying it leaves the volume name ({after.amiga_volume_name!r})")
        check(after.boot_options.swap_df0_with_df1 and after.boot_options.sd_unit0_rw,
              "applying it leaves the boot switches alone")
        check(not after.install_emu68,
              "applying it does not switch Emu68 installation back on")

        #  The machine's own cmdline options share one field with whatever was
        #  typed there, so neither may swallow the other - and the machine's
        #  have to go away again when the switch behind them does.
        a500 = next(i for i, m in enumerate(machines.MACHINES) if m.key == "a500")
        window.quick_machine.set_selected(a500)
        window.quick_trapdoor.set_active(True)
        words = window._quick_config().boot_options.extra_cmdline.split()
        check("move_slow_to_chip" in words and "sd.verbose=1" in words,
              f"both sets of cmdline options survive ({words})")
        window.quick_trapdoor.set_active(False)
        words = window._quick_config().boot_options.extra_cmdline.split()
        check("move_slow_to_chip" not in words and "sd.verbose=1" in words,
              f"turning the switch off removes only its own option ({words})")

        #  Every menu item must be reachable as an action, or choosing it does
        #  nothing and the menu stays open.
        for name in ("save-settings", "load-settings", "forget-session",
                     "inspect-target", "about"):
            check(window.lookup_action(name) is not None,
                  f"menu action {name} exists")

        hdf_index = next(i for i, m in enumerate(MODES)
                         if m[1] is builder.BuildMode.HDF)
        window.mode_row.set_selected(hdf_index)
        window._sync_visibility()
        check(window.hdf_group.get_visible(), "HDF mode reveals the hard disk chooser")
        check(not window.partition_group.get_visible(),
              "HDF mode hides the partition editor (the RDB comes from the image)")
        problems = window.gather().validate()
        check(problems == [], f"HDF mode config is valid ({problems})")
    except Exception as error:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        failures.append(f"exception: {error}")
    finally:
        app.quit()


def main() -> int:
    #  Never share the running application's id, or this test hands its
    #  activation to that instance and silently checks nothing.
    app = ImagerApplication("org.pistorm.ImagerSmokeTest", unique=False)
    app.connect_after("activate", on_activate)  # after do_activate has built the window
    app.run([])
    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all GUI checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
