"""Construct the window for real and exercise the settings round trip.

This runs a genuine GTK application (it needs a display), builds every page,
flips through the task modes and checks that a configuration survives being
written into the widgets and read back out.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
HDF_IMAGE = SCRATCH / "disk.hdf"
HDF_IMAGE.write_bytes(b"\0" * 4096)

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
        check(window.gather().validate() == [], "HDF mode config is valid")
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
