"""Construct the window for real and exercise the settings round trip.

This runs a genuine GTK application (it needs a display), builds every page,
flips through the task modes and checks that a configuration survives being
written into the widgets and read back out.
"""
import dataclasses
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

from pistorm_imager.core import (bootcfg, builder, jobs,  # noqa: E402
                                 machines,
                                 packages as packages_mod)
from pistorm_imager.ui.window import (FRESH_SOURCES,  # noqa: E402
                                      MODES)

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


CLUTTER_IMAGE = SCRATCH / "clutter.hdf"

#  A small image with two Amiga drives in it, for the export page to read.
EXPORT_IMAGE = SCRATCH / "toexport.img"

#  A disc shaped like an AmigaOS 3.9 CD - the trees it carries, empty - so the
#  CD path can be driven without the real 490 MB image being on this machine.
CD_IMAGE = SCRATCH / "AmigaOS39.iso"
from pistorm_imager.core.util import Progress as _Progress  # noqa: E402
QUIET_PROGRESS = _Progress()


def _make_cd_image() -> bool:
    """Master a stand-in AmigaOS 3.9 disc.  False if there is no tool for it."""
    import shutil as _shutil                                # noqa: PLC0415
    import subprocess as _subprocess                        # noqa: PLC0415
    from pistorm_imager.core import amigacd                 # noqa: PLC0415
    tool = _shutil.which("genisoimage") or _shutil.which("mkisofs")
    if tool is None:
        return False
    tree = SCRATCH / "cdtree"
    for layer in amigacd.RELEASES_BY_KEY["3.9"].layers:
        drawer = tree / layer.source
        drawer.mkdir(parents=True, exist_ok=True)
        (drawer / "afile").write_bytes(b"content")
    #  Rock Ridge, because that is what the real 3.9 disc carries and what
    #  the reader has to go through to get the names right.
    _subprocess.run([tool, "-quiet", "-R", "-V", "AmigaOS3.9",
                     "-o", str(CD_IMAGE), str(tree)], check=True)
    return True


def _make_export_image() -> None:
    from pistorm_imager.core import rdb                    # noqa: PLC0415
    geom = rdb.Geometry()
    cyl = geom.cyl_blocks * geom.block_size
    total = 10 * cyl
    parts = rdb.layout(geom, total // geom.block_size,
                       [("DH0", 2 * cyl, rdb.parse_dostype("PFS3")),
                        ("DH1", None, rdb.parse_dostype("PFS3"))])
    table = rdb.Rdb(geometry=geom, partitions=parts,
                    filesystems=[rdb.FileSystem(
                        dostype=rdb.parse_dostype("PFS3"),
                        seglist=b"handler" * 128, version=19 << 16 | 2)],
                    cylinders=total // cyl)
    with open(EXPORT_IMAGE, "wb") as handle:
        handle.truncate(total)
        table.write(handle, 0)


def _make_clutter_hdf() -> None:
    """A small drive carrying one of each thing the clutter pass looks for.

    The empty drive above proves the wiring does not fall over; this one proves
    it actually finds something and that the answer reaches the build.
    """
    from pistorm_imager.core import amigaos, rdb           # noqa: PLC0415
    geometry = rdb.Geometry()
    size = 16 * 1024 * 1024
    cylinders = (size // 512) // geometry.cyl_blocks
    table = rdb.Rdb(geometry=geometry,
                    partitions=[rdb.Partition("DH0", 1, cylinders - 1,
                                              rdb.DOSTYPE_FFS_INTL,
                                              bootable=True)],
                    filesystems=[], cylinders=cylinders)
    #  Read-write: making a volume reads blocks back as it writes them.
    with open(CLUTTER_IMAGE, "w+b") as handle:
        handle.truncate(size)
        table.write(handle, 0)
        part = table.partitions[0]
        volume = amigaos.make_volume(handle,
                                     part.byte_offset(geometry, 0),
                                     part.blocks(geometry), "Sys",
                                     rdb.DOSTYPE_FFS_INTL)
        #  An empty drawer, a drawer of emulator-only scripts, and an assign
        #  pointing at a drawer that is there for now.
        volume.makedirs("Spare")
        uae = volume.makedirs("MyFiles/UAE")
        volume.write_file(uae, "JIT", b"uae-configuration cpu_speed max\n")
        volume.write_file(uae, "Blitter", b"uae-configuration blitter on\n")
        games = volume.makedirs("Games")
        volume.write_file(games, "AGame", b"a real game")
        script = volume.makedirs("S")
        #  One assign into a drawer that survives, and one into the empty
        #  drawer the list offers to remove - so the rule is seen to fire only
        #  when the removal it depends on is actually happening.
        volume.write_file(script, "Assign-Startup",
                          b"Assign >NIL: A-Games: SYS:Games\n"
                          b"Assign >NIL: A-Spare: SYS:Spare\n")
        #  A distribution's own installer that rewrites the boot script. What
        #  it says, and whether it is switched on, depends on whether the build
        #  installs the library it provides - so it is the case that proves a
        #  row is recomputed rather than frozen as first drawn.
        icons = volume.makedirs("Extras/Install/Icons")
        volume.write_file(icons, "Install_Support",
                          b"Copy SYS:S/Startup-Sequence Disable/S/ CLONE\n"
                          b"Copy Stub SYS:S/Startup-Sequence CLONE\n")
        libs = volume.makedirs("Extras/Install/Icons/Enable/Libs")
        volume.write_file(libs, "icon.library", b"the newer one")
        #  Two icons the drive keeps on the desktop: one reachable in a drawer
        #  anybody opens, one that is not.
        tools = volume.makedirs("Tools/Commodities")
        volume.write_file(tools, "CXHandler", b"a commodity")
        volume.write_file(volume.root, ".backdrop",
                          b":Tools/Commodities/CXHandler\n:Games/AGame\n")
        volume.close()


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
_make_clutter_hdf()
_make_export_image()
HAVE_CD = _make_cd_image()

failures: list[str] = []


def pump() -> None:
    """Let GTK settle, so a check sees the state a person would."""
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def check(condition: bool, message: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


def on_activate(app: ImagerApplication) -> None:
    try:
        window = app.window
        check(window is not None, "window constructed")
        check(getattr(window, "_settled", False),
              "the startup work has finished before the window is handed over")
        #  How the window looks before anything is done to it, so "forget the
        #  saved setup" can be held to putting it back exactly here.
        pristine_rows = [dataclasses.asdict(r.spec())
                         for r in window.partition_rows]
        pristine_ticks = {k for k, r in window.package_rows.items()
                          if r.get_active()}

        for index in range(len(MODES)):
            window.mode_row.set_selected(index)
            window._sync_visibility()
        check(True, f"all {len(MODES)} task modes render without error")

        #  Every control the CD group puts on screen has to reach the card.
        #  A save/load round trip cannot catch a field that is never set at
        #  all - it is consistently wrong in both directions and survives the
        #  comparison - so these are read back off gather() itself, which is
        #  what actually writes the card.
        window.mode_row.set_selected(0)                 # a fresh card
        window._sync_visibility()
        #  gather() needs somewhere to write before it will answer at all.
        window.target_row.set_selected(1)               # an image file, not a card
        window.file_row.set_path("/tmp/pistorm-gui-test.img")
        window.quick_accelerator.set_selected(
            list(machines.Accelerator).index(machines.Accelerator.ACCELERATOR))
        window._on_accelerator_changed()
        window.quick_accelerator_cpu.set_selected(
            list(machines.Cpu).index(machines.Cpu.M68060))
        window.boingbag_community.set_active(False)
        window.boingbag_emulator.set_active(False)
        settings = window.gather()
        check(settings.accelerator == machines.Accelerator.ACCELERATOR.value,
              f"the accelerator choice reaches the card: {settings.accelerator}")
        check(settings.accelerator_cpu == machines.Cpu.M68060.value,
              f"and the processor fitted to it: {settings.accelerator_cpu!r}")
        check(settings.boingbag_emulator is False,
              "the FS-UAE switch reaches the card")
        #  Which Amiga the card is for.  The chipset alone cannot answer it,
        #  and the build needs the machine itself - to decide whether its
        #  processor can run 3.5/3.9, and which of a BoingBag's per-machine
        #  drivers to install.  Reaching the build only as a chipset is how
        #  _prepare_os_cd came to ask for a field that was not there.
        check(settings.machine_key == window._machine().key,
              f"the machine reaches the card: {settings.machine_key!r}")
        window.boingbag_emulator.set_active(True)
        check(window.gather().boingbag_emulator is True,
              "and reaches it the other way round too")
        #  A stock machine has no accelerator to describe, so the row that
        #  describes one is not offered and nothing is claimed about it.
        window.quick_accelerator.set_selected(
            list(machines.Accelerator).index(machines.Accelerator.PISTORM))
        window._on_accelerator_changed()
        check(window.gather().accelerator_cpu == "",
              "a PiStorm claims no accelerator processor")
        check(not window.quick_accelerator_cpu.get_visible(),
              "and the row asking for one is hidden")

        #  Emu68 is answered on the Source page, a page *before* the Storage
        #  one carrying the two switches it rules out, so it has to be the
        #  thing that decides - not the other way about.  With no boot
        #  partition there is nowhere to put a Kickstart, a config.txt or a
        #  cmdline.txt either, so those go off the window rather than being
        #  filled in and silently dropped.
        options_page = window.stack.get_page(
            window.stack.get_child_by_name("options"))
        #  The Options page only exists as a page while customising, so that
        #  is the state in which its coming and going means anything.
        window._set_customising(True)
        window.install_emu_row.set_active(True)
        window._sync_visibility()
        check(window.boot_only_row.get_sensitive()
              and not window.amiga_only_row.get_sensitive(),
              "with Emu68 on, only the boot-only card can be asked for")
        window.install_emu_row.set_active(False)
        window._sync_visibility()
        check(window.amiga_only_row.get_sensitive()
              and not window.boot_only_row.get_sensitive(),
              "with Emu68 off, only the drives-only card can be")
        window.amiga_only_row.set_active(True)
        check(window.gather().amiga_only,
              "and the drives-only choice reaches the card")
        check(not window.group_kickstart.get_visible()
              and not window.boot_group.get_visible()
              and not options_page.get_visible(),
              "a card with no boot partition stops asking about one")
        #  And turning Emu68 back on undoes it rather than leaving a
        #  contradiction the build would have to settle.
        window.install_emu_row.set_active(True)
        window._sync_visibility()
        check(not window.amiga_only_row.get_active()
              and not window.gather().amiga_only,
              "turning Emu68 back on withdraws the drives-only card")
        check(window.group_kickstart.get_visible()
              and window.boot_group.get_visible()
              and options_page.get_visible(),
              "and brings back everything that lives on the boot partition")

        #  The community pack is separable, so the two states have to differ.
        window.os_cd_row.set_path("")
        with_none = window.gather().boingbags
        check(with_none == [],
              f"no CD means no BoingBags to name: {with_none}")

        #  A CD carries the whole operating system, so a build taking one must
        #  not still be asking for floppies.  Reported from the running app:
        #  "I am selecting OS3.9 from CD but the config won't apply unless I
        #  select a floppy disc folder!" - Apply stayed off with a 3.9 disc
        #  chosen and nothing actually missing.
        if not HAVE_CD:
            print("  skip  no genisoimage, so the CD checks are not run")
        else:
            window.quick_system_source.set_selected(FRESH_SOURCES.index("cd"))
            window._on_source_changed()
            window.os_cd_row.set_path(str(CD_IMAGE))
            window._on_os_cd_chosen()
            check(not window.adf_row.path,
                  "the floppy folder is genuinely empty for this check")
            wanted = window._missing_choices()
            floppies = [item for item in wanted
                        if "floppy" in item or "Workbench disks" in item]
            check(not floppies,
                  f"a CD install does not ask for floppies: {floppies}")
            check(window.gather().os_cd == str(CD_IMAGE),
                  "and the CD reaches the build")
            check(window.gather().os_cd_release == "3.9",
                  f"with its release read off the disc: "
                  f"{window.gather().os_cd_release!r}")
            #  Choosing the CD source and then no CD has to say so, rather
            #  than falling silent because the floppy question no longer
            #  applies either.
            window.os_cd_row.set_path("")
            window._on_os_cd_chosen()
            asked = window._missing_choices()
            check(any("CD image" in item for item in asked),
                  f"a CD install with no CD asks for one: {asked}")
            window.os_cd_row.set_path(str(CD_IMAGE))
            window._on_os_cd_chosen()

            #  A row's subtitle is Pango markup, so a label carrying an
            #  ampersand - "BoingBags 3 & 4" - makes GTK refuse the whole
            #  string and keep whatever the row said before.  Reported as
            #  "says updates found, but then also says no folder - but I
            #  selected a folder".
            packs = SCRATCH / "boingbags"
            packs.mkdir(exist_ok=True)
            for name in ("BoingBag39-1.lha", "BB1-4.lha"):
                (packs / name).write_bytes(b"not a real archive")
            window.boingbag_row.set_path(str(packs))
            found = window.boingbag_found.get_subtitle() or ""
            check("archive(s)" in found and "No folder" not in found,
                  f"the packs found are named, not swallowed: {found!r}")

            #  What is providing the processor is a person's choice, so a
            #  quick setup must hand it back rather than reset it.  Reported
            #  as "doesn't persist the accelerator type - defaults to
            #  pistorm".
            window.quick_accelerator.set_selected(
                list(machines.Accelerator).index(
                    machines.Accelerator.ACCELERATOR))
            window._on_accelerator_changed()
            window.quick_accelerator_cpu.set_selected(
                list(machines.Cpu).index(machines.Cpu.M68030))
            saved = window.gather()
            kept = window._keep_other_pages(
                dataclasses.replace(saved, accelerator="pistorm",
                                    accelerator_cpu="", os_cd=""),
                saved)
            check(kept.accelerator == machines.Accelerator.ACCELERATOR.value,
                  f"a quick setup keeps the accelerator: {kept.accelerator}")
            check(kept.accelerator_cpu == machines.Cpu.M68030.value,
                  f"and its processor: {kept.accelerator_cpu!r}")
            check(kept.os_cd == str(CD_IMAGE),
                  "and the CD it was going to install from")

            #  And a loaded setup has to put them back into the widgets.
            window.quick_accelerator.set_selected(
                list(machines.Accelerator).index(machines.Accelerator.PISTORM))
            window._on_accelerator_changed()
            window.apply(saved)
            check(window._accelerator() is machines.Accelerator.ACCELERATOR,
                  f"a loaded setup restores the accelerator: "
                  f"{window._accelerator().value}")
            check(window.os_cd_row.path == str(CD_IMAGE),
                  "and the CD image it named")

            #  And the other direction: a path left in the row after switching
            #  back to floppies must not quietly install from it.
            window.quick_system_source.set_selected(FRESH_SOURCES.index("adf"))
            window._on_source_changed()
            check(window.gather().os_cd == "",
                  "switching back to floppies stops using the CD")

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

        #  A layout that arrived with a saved setup is the user's own. It used
        #  to be recorded as though the quick settings had derived it, so the
        #  next relayout replaced the drives someone had arranged - four
        #  partitions saved, one generic layout loaded.
        #  apply() writes every row from the config it is given, so anything a
        #  later check depends on has to be put back afterwards.
        kept_hdf = window.hdf_row.get_path() if hasattr(
            window.hdf_row, "get_path") else window.hdf_row.path
        saved = builder.BuildConfig(
            target=str(SCRATCH / "restored.img"),
            amiga_partitions=[
                builder.AmigaPartitionSpec(name="DH0", volume_name="Workbench",
                                      size=10 * 1024 ** 3, bootable=True),
                builder.AmigaPartitionSpec(name="DH1", volume_name="Games",
                                      size=20 * 1024 ** 3),
                builder.AmigaPartitionSpec(name="DH2", volume_name="Demos",
                                      size=10 * 1024 ** 3),
                builder.AmigaPartitionSpec(name="DH3", volume_name="Work",
                                      size=None),
            ])
        window.apply(saved, keep_partitions=True)
        check([r.spec().volume_name for r in window.partition_rows]
              == ["Workbench", "Games", "Demos", "Work"],
              "a loaded layout reaches the rows")
        #  Anything that would normally redraw the layout must now leave it be.
        window._relayout_partitions()
        names = [r.spec().volume_name for r in window.partition_rows]
        check(names == ["Workbench", "Games", "Demos", "Work"],
              f"a loaded layout survives a relayout ({names})")
        window.quick_pimiga.set_path(str(SCRATCH / "pimiga"))
        names = [r.spec().volume_name for r in window.partition_rows]
        check(names == ["Workbench", "Games", "Demos", "Work"],
              f"a loaded layout survives a quick-setting change ({names})")
        check(window._hand_edited_partitions() is not None,
              "a loaded layout counts as the user's when Apply is pressed")
        window.quick_pimiga.set_path("")

        #  Forgetting the saved setup puts the window back as it started.
        #  Deleting the file alone left everything on screen exactly as it
        #  was, and clearing the widgets by hand left the storage behind.
        window.quick_hdf.set_path(str(HDF_IMAGE))
        window.quick_trapdoor.set_active(True)
        window.extra_row.set_text("sd.verbose=1")
        window.vc4_row.set_value(64)
        window._add_partition()
        window._add_partition()
        window._on_forget_session(None)
        check(window.quick_hdf.path == "" and window.extra_row.get_text() == "",
              "forgetting clears what was chosen")
        check(not window.quick_trapdoor.get_active()
              and window.vc4_row.get_value() == 0,
              "and the switches go back to their defaults")
        after = [r.spec() for r in window.partition_rows]
        #  Not "the same rows as a window that has just opened" - a target has
        #  been chosen since, and the layout follows it. What matters is that
        #  the two drives added by hand are gone and the settings own the
        #  layout again, which is what leaving it alone got wrong.
        check(after == window._quick_layout()
              and window._hand_edited_partitions() is None,
              f"the storage layout goes back to the settings ({len(after)} "
              f"rows, {len(pristine_rows) + 2} before forgetting)")
        check({k for k, r in window.package_rows.items() if r.get_active()}
              == pristine_ticks,
              "and the software ticks are the ones it opened with")

        #  Hand the layout back to the quick settings, or every check after
        #  this one is testing a deliberately protected layout.
        #  The software choices were saved by gather() and never put back,
        #  so loading a setup cleared every tick.
        with_packages = dataclasses.replace(
            saved, package_keys=["whdload", "lha"])
        window.apply(with_packages, keep_partitions=True)
        ticked = {k for k, r in window.package_rows.items() if r.get_active()}
        check({"whdload", "lha"} <= ticked,
              f"the chosen software is restored ({sorted(ticked)})")
        check("newicons" not in ticked,
              "software that was not chosen stays off")
        back = window.gather()
        check(set(back.package_keys) >= {"whdload", "lha"},
              f"and survives a round trip ({back.package_keys})")
        for row in window.package_rows.values():
            row.set_active(False)

        #  Which copy wins when an imported drive already has a program that
        #  was ticked is the user's choice, and it has to survive the round
        #  trip like any other.
        window.mode_row.set_selected(
            next(i for i, m in enumerate(MODES)
                 if m[1] is builder.BuildMode.FRESH))
        window.partition_rows[0].hdf_row.set_path(str(HDF_IMAGE))
        window._sync_visibility()
        check(window.replace_older_row.get_visible(),
              "importing a drive raises the question of which copy wins")
        check(window.gather().replace_older_software,
              "and the newer release is the default answer")
        window.replace_older_row.set_active(False)
        check(not window.gather().replace_older_software,
              "keeping the drive's own copy reaches the build")
        window.replace_older_row.set_active(True)
        for row in window.partition_rows:
            row.hdf_row.set_path("")
        window._sync_visibility()
        check(not window.replace_older_row.get_visible(),
              "and it is not asked when nothing is being imported")

        #  "Adapt the display after writing" sat on the image chooser, which
        #  a build that partitions the card never shows - so a card whose DH0
        #  came from an .hdf had no way to ask for it. It lives with the
        #  display now, on the Amiga page.
        group = window.patch_display_row.get_ancestor(Adw.PreferencesGroup)
        check(group.get_ancestor(Adw.PreferencesPage) is window.page_amiga,
              "the display switch is on the Amiga page")
        fresh_index = next(i for i, m in enumerate(MODES)
                           if m[1] is builder.BuildMode.FRESH)
        window.mode_row.set_selected(fresh_index)
        window.quick_hdf.set_path("")
        for row in window.partition_rows:
            row.hdf_row.set_path("")
        window._sync_visibility()
        check(not window.display_group.get_visible(),
              "a Workbench from floppies is not asked about it")
        window.partition_rows[0].hdf_row.set_path(str(HDF_IMAGE))
        window._sync_visibility()
        check(window.display_group.get_visible(),
              "a drive imported into DH0 is")
        for row in window.partition_rows:
            row.hdf_row.set_path("")
        window._sync_visibility()

        #  The card is written from gather(), and gather() built its boot
        #  options from the widgets alone. enable_slow_ram has no widget - the
        #  machine decides it - so every card went out without
        #  enable_c0_slow, and move_slow_to_chip had nothing to move: a
        #  machine told to give Workbench a megabyte of chip RAM came up with
        #  512K. Found on a real card's cmdline.txt.
        for index, machine in enumerate(machines.MACHINES):
            if machine.key == "a500ecs":
                window.quick_machine.set_selected(index)
        window._on_machine_changed()
        window.quick_trapdoor.set_active(True)
        line = window.gather().boot_options.cmdline()
        check("move_slow_to_chip" in line and "enable_c0_slow" in line,
              f"the trapdoor RAM is mapped as well as moved ({line})")
        window.quick_trapdoor.set_active(False)
        line = window.gather().boot_options.cmdline()
        check("enable_c0_slow" in line and "move_slow_to_chip" not in line,
              f"and the ranges stay mapped without the choice ({line})")

        #  Choosing a display that draws on the Pi's HDMI is choosing the
        #  RTG subsystem with it. Nothing rebuilt the software list when the
        #  display changed, so ticking "both outputs" left Picasso96 off and
        #  the card had no RTG screen modes at all.
        from pistorm_imager.core.machines import Display as _Display
        for wanted in (_Display.BOTH, _Display.RTG_HDMI):
            for index, display in enumerate(_Display):
                if display is wanted:
                    window.quick_display.set_selected(index)
            window._on_display_changed()
            picasso = window.package_rows["picasso96"]
            check(picasso.get_active() and not picasso.get_sensitive(),
                  f"{wanted.name} brings Picasso96 with it and holds it on")
            check("picasso96" in window._chosen_packages(),
                  f"and it reaches the build for {wanted.name}")
        for index, display in enumerate(_Display):
            if display is _Display.NATIVE:
                window.quick_display.set_selected(index)
        window._on_display_changed()
        check(not window.package_rows["picasso96"].get_active(),
              "and a native-only display does not carry it")

        #  The software now has a page of its own; it used to share the
        #  Amiga page with the model, the Kickstart and the Workbench disks,
        #  which are facts about the hardware rather than a shopping list.
        from gi.repository import Adw as _A                   # noqa: PLC0415
        group = window.package_rows["whdload"].get_ancestor(_A.PreferencesGroup)
        check(group.get_ancestor(_A.PreferencesPage) is window.page_packages,
              "the software lives on the Packages page")
        check(window.stack.get_child_by_name("packages") is not None,
              "and the Packages page is in the switcher")

        #  Two packages doing one job are alternatives, and the user is asked
        #  before either is taken away.
        check(packages_mod.CATALOGUE_BY_KEY["deficons"].role
              == packages_mod.CATALOGUE_BY_KEY["newicons"].role != "",
              "DefIcons and NewIcons are both a default-icon system")
        window.package_rows["newicons"].set_active(False)
        window.package_rows["deficons"].set_active(True)
        window.package_rows["newicons"].set_active(True)
        check(sorted(window._rivals("newicons")) == ["deficons"],
              f"the rival is found ({window._rivals('newicons')})")
        check(window.package_rows["deficons"].get_active(),
              "and nothing is removed without being asked")
        check(window._rivals("whdload") == [],
              "a package with no rival raises no question")
        window.package_rows["newicons"].set_active(False)

        #  SysInfo, whose 4.0 carries a guru that Aminet still ships a
        #  patch for - fixed in 4.4, which its Aminet address serves.
        check("sysinfo" in window.package_rows, "SysInfo is on offer")

        #  Dependencies are linked both ways, and a package worth having on
        #  its own is not dragged off with the thing that needed it.
        rows = window.package_rows
        for row in rows.values():
            row.set_active(False)
        rows["igame"].set_active(True)
        check(all(rows[k].get_active() for k in
                  ("mui", "mcc_nlist", "mcc_texteditor", "mcc_urltext")),
              "ticking iGame ticks everything it needs")
        rows["mui"].set_active(False)
        check(not rows["igame"].get_active(),
              "turning MUI off turns off what cannot work without it")
        check(not rows["mcc_nlist"].get_active(),
              "and the classes that were only there for it")
        rows["igame"].set_active(True)
        rows["igame"].set_active(False)
        check(not rows["mcc_nlist"].get_active(),
              "dropping iGame drops the classes nothing else wants")
        check(rows["mui"].get_active(),
              "but MUI stays: it is worth having on its own")
        for row in rows.values():
            row.set_active(False)

        #  A size typed for a card is a guess, and "125G" means 125 GiB - nine
        #  gigabytes more than a card sold as 125 GB. When a card is in front
        #  of us its capacity is known, so it is used and the box is closed.
        from pistorm_imager.core import devices as _devices
        card = _devices.Device(path="/dev/nonexistent-test", name="mmcblk0",
                               size=125_000_000_000, model="TestCard",
                               vendor="", transport="sd", removable=True,
                               hotplug=True, read_only=False, partitions=[])
        window.device_list = [card]
        from pistorm_imager.ui.window import combo as _combo
        labels = _combo(["Select a card", card.description])
        window.device_row.set_model(labels)
        window.quick_device.set_model(_combo(["Select a card",
                                              card.description]))
        window.target_row.set_selected(0)
        window.quick_target.set_selected(0)
        window.quick_card_size.set_text("125G")          # the trap: 125 GiB
        window.quick_device.set_selected(1)
        check(not window.quick_card_size.get_sensitive(),
              "the size box is closed while writing to a card")
        check(window.gather().image_size == card.size,
              f"the card's own capacity is the size "
              f"({window.gather().image_size} vs {card.size})")
        check("125" in window.quick_card_size.get_title()
              and "GB" in window.quick_card_size.get_title(),
              f"the card is named with both readings ({window.quick_card_size.get_title()!r})")
        window.quick_device.set_selected(0)
        window.target_row.set_selected(1)
        window.quick_target.set_selected(1)
        check(window.quick_card_size.get_sensitive(),
              "and opens again for an image file")
        check("32GB as cards are sold" in window.quick_card_size.get_title(),
              f"which says which unit it means ({window.quick_card_size.get_title()!r})")
        window.quick_card_size.set_text("125G")
        window._show_size()
        said = window.quick_size_info.get_subtitle()
        check("binary" in said and "125GB" in said,
              f"a bare G is called out as binary ({said!r})")
        window.quick_card_size.set_text("125GB")
        window._show_size()
        said = window.quick_size_info.get_subtitle()
        check("binary" not in said,
              f"and an explicit GB is left alone ({said!r})")
        window.device_list = []

        #  The whole setup, not one field at a time. Three separate things
        #  were saved faithfully and never restored - the drives, the software
        #  and the Emu68 release - and each was found only when somebody
        #  noticed it missing. This compares every field at once.
        window.target_row.set_selected(1)
        window.quick_target.set_selected(1)
        window.file_row.set_path(str(SCRATCH / "roundtrip.img"))
        window.quick_file.set_path(str(SCRATCH / "roundtrip.img"))
        window.file_size_row.set_text("40GB")
        for key in ("whdload", "lha"):
            if key in window.package_rows:
                window.package_rows[key].set_active(True)
        saved_config = window.gather()
        saved_state = window.interface_state()
        session = SCRATCH / "session.json"
        jobs.save_session(saved_config, saved_state, session)

        #  Scramble everything the load has to put back.
        window.file_size_row.set_text("8GB")
        window.file_row.set_path(str(SCRATCH / "somewhere-else.img"))
        for row in window.package_rows.values():
            row.set_active(False)
        loaded, state, _reduced = jobs.load_session(session)
        window._apply_saved(loaded, state)
        back = window.gather()

        differences = []
        for field in dataclasses.fields(saved_config):
            want, got = (getattr(saved_config, field.name),
                         getattr(back, field.name))
            if field.name == "package_keys":
                want, got = sorted(want), sorted(got)
            if field.name == "amiga_partitions":
                want = [dataclasses.asdict(x) for x in want]
                got = [dataclasses.asdict(x) for x in got]
            if want != got:
                differences.append(f"{field.name}: saved {want!r}, back {got!r}")
        check(not differences,
              "every field of a saved setup survives the round trip"
              + ("" if not differences else ": " + "; ".join(differences)))

        #  A saved setup names the Emu68 build it was made against, and the
        #  list it has to be found in arrives from GitHub after the setup
        #  does. Falling to the newest stable one swapped a card built on a
        #  beta onto a different Emu68 without saying so.
        from pistorm_imager.core import emu68 as _emu68
        assets = ["v1.0.7-Emu68-pistorm.zip", "v1.1.0-beta.1-Emu68-pistorm.zip"]
        window.releases = [
            _emu68.Release(tag="v1.1.0-beta.1", name="1.1 beta 1",
                           prerelease=True, published="2025-01-02",
                           assets=assets),
            _emu68.Release(tag="v1.0.7", name="1.0.7", prerelease=False,
                           published="2024-01-01", assets=assets),
        ]
        window.variant_row.set_selected(
            [v.key for v in _emu68.VARIANTS].index("pistorm"))
        beta = dataclasses.replace(window.gather(), release_tag="v1.1.0-beta.1")
        window.apply(beta, keep_partitions=True)
        check(window.gather().release_tag == "v1.1.0-beta.1",
              f"a saved Emu68 release is restored ({window.gather().release_tag!r})")
        #  And a setup that named none still gets the newest stable build.
        plain = dataclasses.replace(window.gather(), release_tag="")
        window.apply(plain, keep_partitions=True)
        check(window.gather().release_tag == "v1.0.7",
              f"with none saved, the newest stable is chosen ({window.gather().release_tag!r})")
        #  The summary is written before that list arrives, so it says an
        #  Emu68 release is still needed; nothing rewrote it once one was
        #  there, and a loaded setup sat on "Still needed" for ever.
        window.releases = []
        rewrites = []
        real_update = window._update_summary
        window._update_summary = lambda: rewrites.append(1)
        window._releases_loaded([
            _emu68.Release(tag="v1.0.7", name="1.0.7", prerelease=False,
                           published="2024-01-01", assets=assets)])
        window._releases_failed("no network")
        window._update_summary = real_update
        check(len(rewrites) == 2,
              f"the summary is rewritten when the Emu68 list settles ({rewrites})")
        window.releases = []
        window._update_summary()
        #  Hand the layout back to the quick settings and put the hard disk
        #  chooser back: every check after this one depends on both.
        window._derived_partitions = [r.spec() for r in window.partition_rows]
        window._relayout_partitions()
        window.hdf_row.set_path(kept_hdf)

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
        check(saved["machine"] == "a600",
              "interface state captures the hardware choices")
        #  The target and the card size belong to the configuration. Kept here
        #  as well they went stale the moment either was set on its own page,
        #  and this state is applied last, so the stale copy won.
        check("card_size" not in saved and "target_kind" not in saved
              and "image_path" not in saved,
              f"the config's own settings are not duplicated ({sorted(saved)})")

        window.quick_machine.set_selected(0)
        window.quick_pimiga.set_path("")
        window.apply_interface_state(saved)
        restored = window.interface_state()
        check(restored["machine"] == "a600", "machine is restored")
        check(restored["pimiga_folder"] == saved["pimiga_folder"],
              "folders are restored")

        #  A 125 GiB image target, then a session saved when the quick screen
        #  still said "SD card" at 59 GiB: the configuration has to win.
        big = dataclasses.replace(
            window.gather(), target=str(SCRATCH / "big.img"),
            target_is_device=False, image_size=125 * 1024 ** 3)
        window.apply(big, keep_partitions=True)
        window.apply_interface_state(dict(saved, card_size="59.48G",
                                          target_kind=0,
                                          image_path="/tmp/somewhere-else.img"))
        check(window.quick_card_size.get_text().startswith("125"),
              f"the card size survives loading ({window.quick_card_size.get_text()!r})")
        check(window.gather().target == str(SCRATCH / "big.img"),
              f"the target survives loading ({window.gather().target!r})")
        check(window.gather().image_size == 125 * 1024 ** 3,
              f"and so does its size ({window.gather().image_size})")
        #  apply() protects the layout it was given; hand it back, or the
        #  checks below are testing a layout deliberately left alone.
        window._derived_partitions = [r.spec() for r in window.partition_rows]
        window.quick_card_size.set_text("32G")

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
        # ---------------------------------------------- the clutter list
        print("\nclutter the card is better off without")
        window.quick_hdf.set_path(str(CLUTTER_IMAGE))
        window._refresh_clutter()
        offered = {where: row.get_active()
                   for where, row in window.clutter_rows.items()}
        check(bool(offered), f"found on a drive that has some: {sorted(offered)}")
        check("Spare" in offered, "the empty drawer is offered")
        check("MyFiles/UAE" in offered,
              "the drawer of emulator-only scripts is offered")
        check(window.clutter_group.get_visible(),
              "and the group is shown when there is something in it")
        #  What is ticked has to reach the card, or the list is decoration.
        for row in window.clutter_rows.values():
            row.set_active(True)
        leaving = window.gather().leave_out
        check("Spare" in leaving and "MyFiles/UAE" in leaving,
              f"ticked clutter reaches leave_out: {leaving}")
        #  ...and with those going, the assign pointing into one is reported.
        window._refresh_clutter()
        assigns = [w for w in window.clutter_rows if "Assign" in w]
        check(any("A-Spare" in w for w in assigns),
              f"the assign into a removed drawer is reported: {assigns}")
        check(not any("A-Games" in w for w in assigns),
              "and the one into a drawer that stays is left alone")
        for row in window.clutter_rows.values():
            row.set_active(False)
        window.quick_hdf.set_path("")
        window._refresh_clutter()
        #  The row's wording and its default both depend on the rest of the
        #  page. Created once and then skipped, it kept the wording and the
        #  switch it was born with - so a card went out still carrying an
        #  installer that had bricked one.
        window.quick_hdf.set_path(str(CLUTTER_IMAGE))
        for key, row in window.package_rows.items():
            if key == "iconlib":
                row.set_active(False)
        window._refresh_clutter()
        risky = window.clutter_rows.get("Extras/Install/Icons")
        check(risky is not None,
              f"the boot-script installer is offered: {sorted(window.clutter_rows)}")
        if risky is not None:
            check(not risky.get_active(),
                  "and is only a question while nothing replaces it")
            for key, row in window.package_rows.items():
                if key == "iconlib" and row.get_sensitive():
                    row.set_active(True)
            window._refresh_clutter()
            risky = window.clutter_rows.get("Extras/Install/Icons")
            check(risky.get_active(),
                  "choosing the icon library switches it on")
            check("already installs" in risky.get_subtitle(),
                  f"and says why: {risky.get_subtitle()}")
            #  An answer the user has given is not overwritten.
            risky.set_active(False)
            window._refresh_clutter()
            check(not window.clutter_rows["Extras/Install/Icons"].get_active(),
                  "an answer you have given survives a refresh")

        window.quick_hdf.set_path("")
        window._refresh_clutter()
        check(not window.clutter_group.get_visible(),
              "and the group hides again with no drive chosen")

        # ---------------------------------------- a card with no Amiga drive
        #  The pinned rule for this project: an option on screen that does not
        #  change the card is worse than no option, because it looks honoured.
        print("\na card with no Amiga drive")
        window.mode_row.set_selected(0)
        window._sync_visibility()
        before = len(window.partition_rows)
        window.boot_only_row.set_active(True)
        built = window.gather()
        check(built.boot_only, "the switch reaches the build")
        check(built.amiga_partitions == [],
              f"and no drives go with it: {built.amiga_partitions}")
        check(not window.partition_group.get_visible(),
              "the layout is hidden while it cannot apply")
        check(len(window.partition_rows) == before,
              "but the rows are kept, not destroyed")
        check(built.validate() == [],
              f"no drives is valid, not an error: {built.validate()}")
        window.boot_only_row.set_active(False)
        back = window.gather()
        check(not back.boot_only and len(back.amiga_partitions) == before,
              "and switching back restores exactly the layout that was there")
        check(window.partition_group.get_visible(), "with the rows on screen again")

        # ------------------------- one drive, several lists, one set of answers
        #  The page shows the same drive through several lists, and they
        #  describe the same facts. Assembled independently they contradicted
        #  each other: AWeb was switched on under "older copies", meaning
        #  remove it, and switched on under "already installed on the drive",
        #  meaning keep it, and moving either switch did nothing to the other.
        print("\none drive, several lists, one set of answers")

        cover = type(window)._covered_by
        check(cover("Programs/Thing", ["Programs/Thing"]),
              "a path being dropped is covered by itself")
        check(cover("Programs/Thing/Sub", ["Programs/Thing"]),
              "and so is anything inside it")
        check(not cover("Programs/Thingamajig", ["Programs/Thing"]),
              "but a longer name is a different drawer")
        check(not cover("Programs/Thing", ["Programs/Other"]),
              "and an unrelated drawer is not covered")

        #  Every removal list feeds the one shared answer, not just the one
        #  the original exclusion happened to be written for.
        class _Row:
            def __init__(self, on): self._on = on
            def get_active(self): return self._on
        keep = dict(window.older_rows), dict(window.broken_rows), \
            dict(window.clutter_rows), dict(window.arrives_rows)
        window.older_rows = {"Programs/Older": _Row(True)}
        window.broken_rows = {"Programs/Broken": _Row(True)}
        window.clutter_rows = {"Programs/Clutter": _Row(True),
                               "Programs/Kept": _Row(False)}
        window.arrives_rows = {}
        removing = set(window._being_removed())
        for where in ("Programs/Older", "Programs/Broken", "Programs/Clutter"):
            check(where in removing, f"{where} counts as being removed")
        check("Programs/Kept" not in removing,
              "a removal row switched off does not count")
        window.older_rows, window.broken_rows, window.clutter_rows, \
            window.arrives_rows = keep

        #  And the tie itself, driven through the real window on the real
        #  drive when it is here: answering one list re-derives the other.
        drive = Path.home() / "Downloads/ClassicWB_FULL_v28/System.hdf"
        if drive.exists():
            window.quick_hdf.set_path(str(drive))
            #  Every package that suits this machine, so the older-copy search
            #  has something to supersede whatever the drive happens to carry.
            #  Nothing is named: the ticks come from the catalogue and the
            #  answers from the drive, so this holds for any setup.
            was_settling = getattr(window, "_settling_packages", False)
            window._settling_packages = True
            ticked = {}
            try:
                for key, row in window.package_rows.items():
                    ticked[key] = row.get_active()
                    if row.get_sensitive():
                        row.set_active(True)
            finally:
                window._settling_packages = was_settling
            window._refresh_older_copies()
            window._refresh_what_cannot_work()
            window._refresh_what_arrives()
            clash = [k for k in window.arrives_rows
                     if cover(k, window._being_removed())]
            check(not clash,
                  f"no program is in two lists at once: {clash}")
            picked = next((d for d, r in window.older_rows.items()
                           if r.get_active()), None)
            if picked is not None:
                check(picked not in window.arrives_rows,
                      f"{picked} is not also listed as arriving")
                window.older_rows[picked].set_active(False)
                check(picked in window.arrives_rows,
                      "and it comes back when the removal is switched off")
                window.older_rows[picked].set_active(True)
                check(picked not in window.arrives_rows,
                      "and goes again when it is switched back on")
            else:
                print("  --   nothing on this drive is superseded; tie not driven")
            window._settling_packages = True
            try:
                for key, was in ticked.items():
                    window.package_rows[key].set_active(was)
            finally:
                window._settling_packages = was_settling
            window.quick_hdf.set_path("")
            window._refresh_what_arrives()
        else:
            print("  --   ClassicWB is not on this machine; tie not driven")

        # ------------------------------------- icons on the Workbench desktop
        print("\nicons on the Workbench desktop")
        window.quick_hdf.set_path(str(CLUTTER_IMAGE))
        window._refresh_desktop()
        rows = window.desktop_rows
        check("Tools/Commodities/CXHandler" in rows,
              f"the desktop list is read: {sorted(rows)}")
        check(all(r.get_active() for r in rows.values()),
              "everything the drive put there stays until somebody says otherwise")
        check("also in Tools/Commodities" in
              rows["Tools/Commodities/CXHandler"].get_subtitle(),
              "and it says where the icon can be found anyway")
        rows["Tools/Commodities/CXHandler"].set_active(False)
        cfg = window.gather()
        check(cfg.off_desktop == ["Tools/Commodities/CXHandler"],
              f"turning one off reaches the build: {cfg.off_desktop}")
        #  The distinction that matters: off the desktop, not off the card.
        check("Tools/Commodities/CXHandler" not in cfg.leave_out,
              "and it is not confused with leaving the program out")
        rows["Tools/Commodities/CXHandler"].set_active(True)
        window.quick_hdf.set_path("")
        window._refresh_desktop()
        check(not window.desktop_group.get_visible(),
              "and the group hides with no drive chosen")

        check(window.quick_pimiga.path == "" and window.quick_hdf.get_visible(),
              "choosing an image drops the PiMiga folder")
        #  A drive built for another machine is warned about. The check that
        #  says so existed for weeks without ever being called, so this asks
        #  whether the warning reaches the screen, not merely that it exists.
        from pistorm_imager.core import presets as _presets
        original = _presets.check_image_for_machine
        _presets.check_image_for_machine = \
            lambda *a, **k: ["installs display modes this machine cannot produce"]
        try:
            window.quick_hdf.set_path(str(HDF_IMAGE))
            said = window.quick_hdf_info.get_subtitle()
        finally:
            _presets.check_image_for_machine = original
        check("cannot produce" in said,
              f"an image built for another machine is warned about ({said!r})")

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
        check(not window.back_button.get_visible(),
              "and there is nothing to go back to")
        check(not window.bottom_bar.get_visible(),
              "nor anything to summarise or write yet")

        window._set_customising(True)
        visible = {name for name in ("quick", "source", "storage", "amiga",
                                     "options", "target")
                   if window.stack.get_page(
                       window.stack.get_child_by_name(name)).get_visible()}
        check("quick" not in visible and "storage" in visible,
              f"customising shows the workflow and hides the quick start ({visible})")
        check(window.back_button.get_visible()
              and window.bottom_bar.get_visible(),
              "and offers a way back to it, with the summary and Write")
        window._set_customising(False)
        check(window.stack.get_visible_child_name() == "quick",
              "going back returns to the quick start")

        def on_quick_screen() -> set:
            names = {"choices": window.group_choices,
                     "hardware": window.group_hardware,
                     "detected": window.group_detected,
                     "image": window.image_group,
                     "target": window.group_target,
                     "plan": window.group_plan}
            return {name for name, group in names.items()
                    if group.get_visible()
                    and group.get_ancestor(Adw.PreferencesPage)
                    is window.page_quick}

        #  The first screen is the choice and nothing else.
        window._set_customising(False)
        window._set_quick_screen("choices")
        shown = on_quick_screen()
        check(shown == {"choices"},
              f"the first screen shows only the three choices ({shown})")

        window._choose_basic()
        shown = on_quick_screen()
        check(shown == {"hardware", "detected", "target", "plan"}
              and window.back_button.get_visible(),
              f"a basic card shows its own options and a way back ({shown})")

        window._choose_prepared()
        shown = on_quick_screen()
        check(shown == {"image", "target", "plan"}
              and window.back_button.get_visible(),
              f"a prepared system asks only for the image and the card ({shown})")

        def rank(group) -> int:
            """Where a group sits among its siblings on the page."""
            parent = group.get_parent()
            index, child = 0, parent.get_first_child()
            while child is not None:
                if child is group:
                    return index
                index, child = index + 1, child.get_next_sibling()
            return -1

        #  The picker has to come before the summary that describes what it
        #  chose; add() appends, so a group moved here landed last.
        window._choose_prepared()
        check(rank(window.image_group) < rank(window.group_target)
              < rank(window.group_plan),
              f"image {rank(window.image_group)}, card "
              f"{rank(window.group_target)}, plan {rank(window.group_plan)} "
              f"- in that order")

        window._choose_basic()
        check(rank(window.group_hardware) < rank(window.group_detected)
              < rank(window.group_target) < rank(window.group_plan),
              "a basic card reads hardware, disks, card, plan")

        window._set_quick_screen("choices")
        check(window.group_choices.get_visible()
              and not window.back_button.get_visible(),
              "Back returns to the three choices")

        #  One Back, doing whatever going back means where you are - and
        #  always to the choice itself, never to the screen last open.
        window._choose_basic()
        window._go_back()
        check(window.group_choices.get_visible()
              and not window.back_button.get_visible(),
              "Back from a quick screen returns to the choices")
        window._choose_basic()
        window._set_customising(True)
        window._go_back()
        check(not window._customising and window.group_choices.get_visible()
              and not window.back_button.get_visible(),
              "Back from the workflow returns to the choices, not the last screen")

        #  Settings belong on the page they are about.
        window._set_customising(True)
        for group, page_name in ((window.group_hardware, "amiga"),
                                 (window.group_primary, "source"),
                                 (window.group_sizes, "storage")):
            holder = group.get_ancestor(Adw.PreferencesPage)
            found = holder is window.stack.get_child_by_name(page_name)
            check(found, f"a group lives on the {page_name} page")

        #  Writing needs more than a valid configuration: a card written with
        #  no Kickstart boots into nothing.
        window._set_customising(True)
        window.file_row.set_path(str(SCRATCH / "gate.img"))
        window.rom_row.set_path("")
        window._update_summary()
        check(not window.apply_button.get_sensitive()
              and "Kickstart" in window.apply_note.get_text(),
              f"Apply waits for a Kickstart ({window.apply_note.get_text()})")

        rom = Path(__file__).resolve().parent.parent / "samples" / "kickstart"
        roms = sorted(rom.glob("*.rom"))
        if roms:
            window.rom_row.set_path(str(roms[0]))
        from pistorm_imager.core import emu68 as _e
        window.releases = [_e.Release(                # as if the list had loaded
            tag="v1.0.7", name="1.0.7", prerelease=False, published="2024-01-01",
            assets=[f"v1.0.7-Emu68-{v.key}.zip" for v in _e.VARIANTS])]
        window._update_summary()
        check(not window.write_button.get_sensitive(),
              "Write is off until the setup is applied")
        check(window.apply_button.get_sensitive(),
              f"Apply is offered once the choices are made "
              f"({window.apply_note.get_text()})")
        window._on_apply_quick(None)
        check(window.write_button.get_sensitive(),
              "applying enables Write")
        window.partition_rows[0].volume_row.set_text("ChangedAfterApply")
        window._update_summary()
        check(not window.write_button.get_sensitive(),
              "changing anything afterwards disables Write again")
        window._on_apply_quick(None)
        check(window.write_button.get_sensitive(),
              "and applying again re-enables it")

        #  The same block finishes every route: what it will build, with the
        #  button that accepts it underneath.
        check(window.group_plan.get_ancestor(Adw.PreferencesPage)
              is window.page_target,
              "customising finishes on the Target page")
        window._set_customising(False)
        window._choose_basic()
        check(window.group_plan.get_ancestor(Adw.PreferencesPage)
              is window.page_quick,
              "and a quick option finishes on its own screen")
        #  The strip is the last thing in the summary card, under the text.
        card = window.quick_plan.get_parent()
        order, child = [], card.get_first_child()
        while child is not None:
            order.append(child)
            child = child.get_next_sibling()
        check(order and order[0] is window.quick_plan
              and order[-1] is window.apply_row,
              "Apply sits at the end of the summary, underneath it")
        #  Reconsidering the choice withdraws the setup with it.
        window._go_back()
        window._update_summary()
        check(not window.write_button.get_sensitive(),
              "going back withdraws the accepted setup")

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
                     "inspect-target", "check-updates", "about"):
            check(window.lookup_action(name) is not None,
                  f"menu action {name} exists")

        #  The answer is shown without asking GitHub anything in a test.
        from pistorm_imager.core import updates as _u  # noqa: PLC0415
        window._updates_answered(None)
        window._updates_answered(_u.Release("v0.0.1", "Ancient", "old", "u"))
        window._updates_answered(_u.Release("v99.0.0", "Future",
                                            "It flies now", "u"))
        check(True, "every update answer renders without error")

        hdf_index = next(i for i, m in enumerate(MODES)
                         if m[1] is builder.BuildMode.HDF)
        window.mode_row.set_selected(hdf_index)
        window._sync_visibility()
        check(window.hdf_group.get_visible(), "HDF mode reveals the hard disk chooser")
        check(not window.partition_group.get_visible(),
              "HDF mode hides the partition editor (the RDB comes from the image)")
        problems = window.gather().validate()
        check(problems == [], f"HDF mode config is valid ({problems})")
        #  The plan used to walk the partition list, which this task does not
        #  use, and so announced an empty DH0 on a card whose whole point was
        #  the drive in the image.
        window._describe_plan()
        plan = window.quick_plan.get_text()
        check("left empty" not in plan and HDF_IMAGE.name in plan,
              "the plan describes the drives inside the image")

        #  A card built from floppies has to be able to be pointed at the
        #  floppies.  The quick start reported what it had found and offered
        #  no way to correct it, because the choosers live on pages it does
        #  not show - so "build a new card, install from my floppy images"
        #  led to a screen with nowhere to select them.
        from gi.repository import Adw as _Adw
        from pistorm_imager.ui.window import FRESH_SOURCES as _SOURCES

        def group_of(row):
            g = row.get_ancestor(_Adw.PreferencesGroup)
            return g.get_title() if g is not None else "nowhere"

        window._choose_basic()
        pump()
        on_quick = group_of(window.adf_row)
        check(on_quick == group_of(window.quick_found_adf),
              f"the basic screen offers the ADF chooser (it is on {on_quick!r})")
        check(group_of(window.rom_row) == on_quick,
              "the basic screen offers the Kickstart chooser")
        check(group_of(window.quick_system_source) == on_quick,
              "the basic screen offers the install-from-floppies choice")
        #  An earlier check left an .hdf selected, and an .hdf source
        #  correctly rules a floppy install out - so start from a clean one.
        window.quick_hdf.set_path("")
        window.quick_system_source.set_selected(_SOURCES.index("adf"))
        pump()
        check(window.adf_row.get_visible(),
              "choosing a floppy install shows the folder chooser")

        #  And the workflow must get them back, or its own pages come up
        #  with nothing to choose a Kickstart or the disks with.
        window._set_customising(True)
        pump()
        check(group_of(window.rom_row) == "Kickstart ROM",
              f"the Kickstart chooser returns to its page "
              f"(it is on {group_of(window.rom_row)!r})")
        check(group_of(window.adf_row) == "Workbench floppy images",
              f"the ADF chooser returns to its page "
              f"(it is on {group_of(window.adf_row)!r})")
        #  A drive imported from an image can need the Workbench disks as
        #  well - ClassicWB's brings no Workbench of its own - and nothing
        #  asked for them: the demand was made only once a folder had already
        #  been chosen, so a card that needs the disks and has none said
        #  nothing at all and built, unbootable.
        from pistorm_imager.core import presets as _presets
        from pistorm_imager.ui.window import PRIMARY_SOURCES as _PRIMARY
        real_inspect = _presets.inspect_image_system
        _presets.inspect_image_system = lambda path, partition="": (
            _presets.ImageSystem(label="Workbench", found={"bootable": True}))
        try:
            window._floppy_need = {}
            window.adf_row.set_path("")
            window.mode_row.set_selected(
                next(i for i, m in enumerate(MODES)
                     if m[1] is builder.BuildMode.FRESH))
            window.quick_primary.set_selected(_PRIMARY.index("image"))
            window.quick_hdf.set_path(str(HDF_IMAGE))
            pump()
            check(window._imported_needs_floppies(),
                  "a drive that brings no Workbench is recognised")
            wanted = [m for m in window._missing_choices()
                      if "floppy images" in m]
            check(bool(wanted),
                  f"importing such a drive asks for the disks ({wanted})")
            check(group_of(window.adf_row) == "Primary installation",
                  "and the chooser is beside the drive that needs them "
                  f"(it is on {group_of(window.adf_row)!r})")
            window.adf_row.set_path(str(ADF_FOLDER))
            pump()
            check(not [m for m in window._missing_choices()
                       if "floppy images" in m],
                  "and stops asking once they are pointed at")
        finally:
            _presets.inspect_image_system = real_inspect
            window._floppy_need = {}
            window.quick_hdf.set_path("")
            window.quick_primary.set_selected(_PRIMARY.index("default"))
            pump()

        window._set_customising(False)
        pump()

        #  The build log is a window of its own now, not a page of this one:
        #  as a page it inherited whatever size the setup wanted and had to be
        #  resized by hand for every build.
        print("\nthe build log is its own window")
        check(hasattr(window, "progress_window"), "there is a progress window")
        pw = window.progress_window
        check(pw.get_modal(), "it is modal")
        check(pw.get_transient_for() is window, "and belongs to the main window")
        check(pw.get_default_size()[1] >= 600,
              f"sized for reading a log ({pw.get_default_size()})")
        check("progress" not in [window.outer.get_page(c).get_name()
                                 for c in window.outer.observe_children()],
              "and the main window has no progress page left")
        #  It must refuse to close while a build is running.
        window.cancel_button.set_visible(True)
        check(window._on_progress_close(pw) is True,
              "closing is refused while the build runs")
        window.cancel_button.set_visible(False)
        check(window._on_progress_close(pw) is False,
              "and allowed once it has finished")

        #  The suggested load must both tick and untick, for whatever the
        #  machine and screen happen to be now.
        print("\nthe suggested load follows the conditions")
        from pistorm_imager.core import machines as _mm, packages as _pk  # noqa: PLC0415
        for want_rtg in (True, False):
            index = next(i for i, d in enumerate(_mm.Display)
                         if d.uses_rtg == want_rtg)
            window.quick_display.set_selected(index)
            pump()
            #  Tick something the suggestion will not want, to prove it is
            #  turned back off rather than merely left alone.
            noise = [k for k, r in window.package_rows.items()
                     if r.get_sensitive() and not r.get_active()]
            if noise:
                window.package_rows[noise[0]].set_active(True)
                pump()
            window._apply_suggested_packages()
            pump()
            wanted = set(_pk.suggested(
                window._machine(), window._display(),
                networking=bool(window.ssid_row.get_text().strip())))
            chosen = set(window._chosen_packages())
            #  Everything suggested is on...
            short = sorted(k for k in wanted if k not in chosen)
            check(not short, f"rtg={want_rtg}: nothing suggested is left off "
                             f"({short})")
            #  ...and anything extra is there because something needs it, or
            #  because the display holds it on.
            extra = sorted(k for k in chosen - wanted
                           if not any(k in _pk.expand([w]) for w in wanted)
                           and not _pk.CATALOGUE_BY_KEY[k].essential)
            check(not extra, f"rtg={want_rtg}: nothing unwanted is left on "
                             f"({extra})")

        #  A switch that is on and cannot be moved has to say why, at the
        #  front: Picasso96 is held on by choosing an RTG display, and the
        #  reason used to arrive after three hundred characters of notes.
        print("\na locked package says why")
        from pistorm_imager.core import machines as _m           # noqa: PLC0415
        rtg = next(i for i, d in enumerate(_m.Display) if d.uses_rtg)
        was_display = window.quick_display.get_selected()
        window.quick_display.set_selected(rtg)
        pump()
        locked = [(k, r) for k, r in window.package_rows.items()
                  if not r.get_sensitive() and r.get_active()]
        check(bool(locked),
              f"something is held on by this display ({[k for k, _ in locked]})")
        for key, row in locked:
            check(row.get_subtitle().startswith("Required by the display"),
                  f"{key} says why first: {row.get_subtitle()[:60]!r}")
        window.quick_display.set_selected(was_display)
        pump()

        #  What the drive already carries, offered one at a time.
        print("\nsoftware the drive arrives with")
        window.quick_hdf.set_path(str(HDF_IMAGE))
        pump()
        check(not window.arrives_rows,
              f"an empty drive offers nothing ({sorted(window.arrives_rows)})")
        check(not window.arrives_group.get_visible(),
              "and the group stays hidden")
        real = Path.home()/"Downloads/ClassicWB_FULL_v28/System.hdf"
        if real.exists():
            window.quick_hdf.set_path(str(real))
            pump()
            check(len(window.arrives_rows) > 20,
                  f"ClassicWB's own software is listed "
                  f"({len(window.arrives_rows)} programs)")
            check(any(k.endswith("/FinalWriter") for k in window.arrives_rows),
                  "FinalWriter among them")
            check(not any(k.startswith("Tools/") or k.startswith("Utilities/")
                          for k in window.arrives_rows),
                  "and Workbench's own tools are not offered for deletion")
            #  One program, one row. FMSsys was in both lists at once - on
            #  here meaning keep it, on in the other meaning remove it.
            both = set(window.arrives_rows) & set(window.broken_rows)
            check(not both, f"nothing is in both lists at once ({sorted(both)})")
            check(any("FMSsys" in k for k in window.broken_rows),
                  "FMSsys is in the one that removes it")
            check(not any("FMSsys" in k for k in window.arrives_rows),
                  "and not in the one that keeps it")
            check(any("FMSsys" in k for k in window.gather().leave_out),
                  "so the build is told to leave it out")
            key = next(k for k in window.arrives_rows
                       if k.endswith("/FinalWriter"))
            window.arrives_rows[key].set_active(False)
            pump()
            #  Not the only entry: software that cannot work here is in the
            #  same list, switched on by itself.
            check(key in window.gather().leave_out,
                  f"unticking one leaves it out ({window.gather().leave_out})")
            window.arrives_rows[key].set_active(True)
            window.quick_hdf.set_path("")
            pump()
            check(not window.arrives_rows, "and clearing the drive clears it")

        #  Older copies the drive already carries, offered one at a time.
        print("\nolder copies on the drive")
        window.quick_hdf.set_path(str(HDF_IMAGE))
        pump()
        for key in ("sysinfo",):
            row = window.package_rows.get(key)
            if row is not None:
                row.set_active(True)
        pump()
        #  The test drive is an empty one, so nothing should be offered: a
        #  drawer that is not there must never be listed for removal.
        check(not window.older_rows,
              f"nothing offered for a drive that carries none of it "
              f"({sorted(window.older_rows)})")
        check(not window.older_group.get_visible(),
              "and the group stays out of the way")
        gathered = window.gather()
        check(list(gathered.leave_out) == [],
              f"nothing is removed when nothing was offered "
              f"({gathered.leave_out})")
        #  Software the drive carries that cannot work here at all.
        print("\nsoftware that cannot work")
        real0 = Path.home()/"Downloads/ClassicWB_FULL_v28/System.hdf"
        if real0.exists():
            window.quick_hdf.set_path(str(real0))
            pump()
            check(bool(window.broken_rows),
                  f"the check found something ({sorted(window.broken_rows)})")
            check(any("FMSsys" in k for k in window.broken_rows),
                  "FMSsys among it, which cannot mount anything as it ships")
            for key, row in window.broken_rows.items():
                check(row.get_active(), f"{key} defaults to being removed")
                check(len(row.get_subtitle()) > 20,
                      f"and says why: {row.get_subtitle()[:70]!r}")
                break
            out = window.gather().leave_out
            check(all(k in out for k in window.broken_rows),
                  "and they all reach the build")
            first = sorted(window.broken_rows)[0]
            window.broken_rows[first].set_active(False)
            pump()
            check(first not in window.gather().leave_out,
                  "turning one back on keeps it")
            window.broken_rows[first].set_active(True)
            window.quick_hdf.set_path("")
            pump()
            check(not window.broken_rows, "and clearing the drive clears it")

        #  Both lists mean the same thing and must reach the same place: a
        #  drawer of the drive's software switched OFF, and an older copy
        #  switched ON. The second was written to a config field nobody read,
        #  so the whole list did nothing while looking as though it worked.
        real = Path.home()/"Downloads/ClassicWB_FULL_v28/System.hdf"
        if real.exists():
            window.quick_hdf.set_path(str(real))
            for key in ("sysinfo",):
                row = window.package_rows.get(key)
                if row is not None:
                    row.set_active(True)
            pump()
            if window.older_rows:
                drawer, row = sorted(window.older_rows.items())[0]
                row.set_active(True)
                pump()
                check(drawer in window.gather().leave_out,
                      f"a ticked older copy reaches the build ({drawer})")
                row.set_active(False)
                pump()
                check(drawer not in window.gather().leave_out,
                      "and unticking it takes it back out again")
            else:
                check(False, "nothing was offered to compare against")
            window.quick_hdf.set_path("")
            pump()

        window.quick_hdf.set_path("")
        pump()

        #  The size box, which cost a written card. A 64 GB card was shown as
        #  "59.48 GiB", building an image file read that back as 1.6 MB more
        #  than the card holds, and the box was locked so it could not be
        #  corrected.
        print("\nthe card size box")
        from types import SimpleNamespace                        # noqa: PLC0415
        from pistorm_imager.core.util import exact_size_text     # noqa: PLC0415
        from pistorm_imager.ui.window import parse_size          # noqa: PLC0415
        real_size = 63864569856
        from pistorm_imager.ui.window import SELECT_CARD, combo  # noqa: PLC0415
        #  ``path`` is what gather() writes to; without it the stand-in
        #  proved the box was locked but never that the write goes anywhere.
        card = SimpleNamespace(path="/dev/sdz", name="/dev/sdz", size=real_size,
                               label="Test card", model="Test",
                               description="Test card (59.48 GiB)",
                               removable=True)
        window.device_list = [card]
        window.device_row.set_model(combo([SELECT_CARD, card.description]))
        window.quick_device.set_model(combo([SELECT_CARD, card.description]))
        #  Chosen the way the quick setup does it, which is what mirrors onto
        #  the Target page.
        window.quick_target.set_selected(0)            # write to a card
        window.quick_device.set_selected(1)            # the one above
        pump()
        shown = window.file_size_row.get_text()
        check(parse_size(shown) == real_size,
              f"a card's size reads back as exactly itself ({shown!r} -> "
              f"{parse_size(shown):,} of {real_size:,})")
        check(not window.file_size_row.get_sensitive(),
              "and is locked while that card is the target")

        #  The user's move that lost a card: switching to an image file on the
        #  Target page itself, which used only to re-lay out the page.
        window.target_row.set_selected(1)              # ...now an image file
        pump()
        check(window.file_size_row.get_sensitive(),
              "switching to an image file on the Target page unlocks it again")
        check(parse_size(window.file_size_row.get_text()) <= real_size,
              "and the size it is left holding still fits the card")

        #  Now genuinely building an image file, with that card still in the
        #  reader, and a size a megabyte over what it holds.
        window.quick_target.set_selected(1)
        pump()
        window.quick_card_size.set_text(exact_size_text(real_size + 1024 * 1024))
        pump()
        check("too big" in window.quick_size_info.get_subtitle(),
              "a size just over a card in the reader is called out: "
              f"{window.quick_size_info.get_subtitle()[-90:]!r}")
        window.device_list = []
        window.quick_target.set_selected(1)
        window.quick_card_size.set_text("32GB")
        pump()

        #  Choosing a card on the Target page has to survive being chosen.
        #  The mirror between Quick setup and the Target page ran one way,
        #  and selecting a card wrote the card's size into Quick setup's size
        #  box, whose changed handler ran the mirror, which put the card row
        #  back to the placeholder and the "Write to" row back to "SD card
        #  image file". The card was deselected by its own side effect one
        #  signal later, and the build went to a file - on the one path that
        #  destroys a disk, which is the worst place for a control that looks
        #  honoured and is not.
        print("\nchoosing a card actually writes to the card")
        window.device_list = [card]
        labels = [SELECT_CARD] + [c.description for c in window.device_list]
        for row in (window.device_row, window.quick_device):
            row.set_model(combo(labels))
            row.set_selected(0)
        pump()
        #  Start where a person starts when no card was in at launch.
        window.quick_target.set_selected(1)
        pump()
        window.target_row.set_selected(0)
        pump()
        window.device_row.set_selected(1)
        pump()
        check(window.device_row.get_selected() == 1,
              "the card stays selected on the Target page")
        check(window.target_row.get_selected() == 0,
              "and 'Write to' stays on SD card")
        chosen = window.gather()
        check(chosen.target_is_device and chosen.target == "/dev/sdz",
              f"and the build goes to the card: {chosen.target!r} "
              f"(is_device={chosen.target_is_device})")
        #  The choice has to survive the next thing touched on Quick setup.
        window._mirror_target()
        pump()
        check(window.gather().target_is_device,
              "and it survives a later Quick setup change")
        #  Picking on Quick setup still works, and so does going back.
        window.target_row.set_selected(1)
        pump()
        check(not window.gather().target_is_device,
              "switching back to an image file still writes a file")
        #  There used to be a third choice here, "Amiga hard disk image
        #  (.hdf)", which wrote the build's output as one bare drive. A card
        #  carries four, so it could not say which drive it was. It is gone,
        #  and reading drives back out is its own task now.
        check(window.target_row.get_model().get_n_items() == 2,
              "the bare .hdf output option is gone, not merely hidden")
        check(not window._making_hdf(), "and nothing still asks for one")
        window.target_row.set_selected(1)
        pump()
        check(not window.gather().target_is_device,
              "and the two that remain still survive the mirror")
        window.target_row.set_selected(1)
        window.device_list = []
        pump()
        # ------------------------------- exporting drives out of an image
        #  The old answer wrote the build's output as one bare .hdf, which
        #  cannot describe the four drives a PiStorm card carries. This is
        #  the replacement, and it has to reach the job like anything else.
        print("\nexporting drives as .hdf")
        #  Put the window back exactly as it was afterwards: this task hides
        #  every other page while it is chosen, and the checks that follow
        #  are about the quick start.
        was_customising = getattr(window, "_customising", False)
        was_mode = window.mode_row.get_selected()
        export_mode = next(i for i, m in enumerate(MODES)
                           if m[1] is builder.BuildMode.EXPORT)
        window.mode_row.set_selected(export_mode)
        window._sync_visibility()
        pump()
        check(window._mode() is builder.BuildMode.EXPORT, "the task can be chosen")
        pages = {n: window.stack.get_page(window.stack.get_child_by_name(n))
                 for n in ("export", "amiga", "packages", "target")}
        check(pages["export"] is not None and pages["export"].get_visible(),
              "its own page is shown")
        check(not any(pages[n].get_visible() for n in ("amiga", "packages", "target")),
              "and the pages about building a card are not")

        window.export_source.set_path(str(EXPORT_IMAGE))
        pump()
        names = sorted(window.export_rows)
        check(names == ["DH0", "DH1"],
              f"the drives are read out of the image, not guessed: {names}")

        #  Nothing is exported because nobody said otherwise: these write new
        #  files, and a games drive is twenty gigabytes.
        check(not any(r.get_active() for r in window.export_rows.values()),
              "every drive starts unticked")
        window.export_dir.set_path(str(SCRATCH / "exported"))
        pump()
        check(not window.write_button.get_sensitive(),
              "and Export is disabled with none of them chosen")
        window.export_rows["DH1"].set_active(True)
        pump()
        check(window.write_button.get_sensitive(),
              "choosing one enables it")
        window.export_rows["DH0"].set_active(True)
        pump()
        check(window.write_button.get_sensitive(), "and two keep it enabled")
        window.export_rows["DH0"].set_active(False)
        window.export_rows["DH1"].set_active(False)
        pump()
        check(not window.write_button.get_sensitive(),
              "unticking them all disables it again")
        window.export_rows["DH1"].set_active(True)
        pump()
        subtitle = window.export_rows["DH1"].get_subtitle()
        check(".hdf" in subtitle, f"and each says what file it becomes: {subtitle}")

        out = SCRATCH / "exported"
        window.export_dir.set_path(str(out))
        pump()
        job = window.gather()
        check(job.export_drives == ["DH1"],
              f"only the ticked drives reach the job: {job.export_drives}")
        check(job.export_dir == str(out), "and so does where they go")
        check(job.source_image == str(EXPORT_IMAGE),
              "the image comes from this page, not the Source page")
        check(job.validate() == [], f"the job is runnable: {job.validate()}")

        #  The bar carries Back, the summary and the button that runs the
        #  export. Without it the task could be chosen and then neither left
        #  nor performed, which is how it first shipped.
        check(window.bottom_bar.get_visible(), "the bottom bar is on this page")
        check(window.back_button.get_visible(), "with a way back")
        check(window.write_button.get_sensitive(),
              "and the button is usable without applying a setup first")
        check(window.write_button.get_label() == "Export",
              f"and says what it does: {window.write_button.get_label()!r}")
        #  Including before the task is ready: the label names the task, not
        #  the state of it, and an export with no image still said "Write
        #  card" because the label was set after the early return.
        window.export_source.set_path("")
        pump()
        check(window.write_button.get_label() == "Export",
              f"even with nothing chosen yet: {window.write_button.get_label()!r}")
        window.export_source.set_path(str(EXPORT_IMAGE))
        pump()
        #  Choosing an image rebuilds the rows, and they come back unticked -
        #  which is the point of the default, so say so again here.
        window.export_rows["DH1"].set_active(True)
        pump()
        summary = window.summary.get_text()
        check("DH1" in summary and "Export" in summary,
              f"the summary describes the export: {summary!r}")

        #  And it really writes them: the whole point is a file that mounts.
        builder.run_build(job, QUIET_PROGRESS)
        made = sorted(q.name for q in out.glob("*.hdf"))
        check(made == ["DH1.hdf"], f"one self-contained file per drive: {made}")
        from pistorm_imager.core import export as export_mod
        back = export_mod.drives(out / "DH1.hdf")
        check([d.name for d in back] == ["DH1"],
              "and it reads back as the drive it came from")

        #  Back has to leave the task, not just the page: the mode is what
        #  hides everything else.
        window._go_back()
        pump()
        check(window._mode() is not builder.BuildMode.EXPORT,
              "Back leaves the export task")
        export_page = window.stack.get_page(window.stack.get_child_by_name("export"))
        check(not export_page.get_visible(), "and its page with it")
        quick_page = window.stack.get_page(window.stack.get_child_by_name("quick"))
        check(quick_page.get_visible(), "landing back on the first screen")

        window.mode_row.set_selected(was_mode)
        window._sync_visibility()
        window._set_customising(was_customising)
        pump()

        # ------------------------------ a way back from everywhere but the start
        #  The rule: the first screen is a choice and needs no Back, and
        #  every other screen has one. The export page broke it by being a
        #  task the bar had never heard of - and the bar carries the button
        #  that runs the job as well, so that page could be neither left nor
        #  used. Walked here rather than reasoned about, one screen at a time.
        print("\na way back from everywhere but the first screen")
        window._set_customising(False)
        window._set_quick_screen("choices")
        pump()
        check(not window.back_button.get_visible(),
              "the first screen is a choice, and needs no Back")
        check(not window.bottom_bar.get_visible(), "nor a bar to put it in")

        #  And it stays that way when another task is chosen. A session saved
        #  while exporting used to come back with the first screen on top of
        #  the export task: no image chosen, so the bar read "Still needed: No
        #  image to export drives from" and offered Back to where it already
        #  was. _set_customising showed the quick page without asking what the
        #  mode was.
        window.mode_row.set_selected(
            next(i for i, m in enumerate(MODES)
                 if m[1] is builder.BuildMode.EXPORT))
        window._sync_visibility()
        window._set_customising(False)          # what startup does last
        pump()
        quick_page = window.stack.get_page(window.stack.get_child_by_name("quick"))
        check(not quick_page.get_visible(),
              "the first screen does not reappear under another task")
        check(window.stack.get_page(
            window.stack.get_child_by_name("export")).get_visible(),
              "the task that was chosen is enabled")
        #  And is the one actually on screen. Hiding a page does not move the
        #  stack off it: the switcher listed only Export while the quick
        #  page's content was still displayed, which is exactly what the
        #  screenshot showed. Asking which pages are enabled would have passed.
        check(window.stack.get_visible_child_name() == "export",
              f"and is the page actually shown: "
              f"{window.stack.get_visible_child_name()!r}")
        #  A task that writes no card does not survive a restart: the session
        #  records the mode, so quitting inside Export reopened there - the
        #  one task that hides the first screen. This is the rule startup
        #  applies after restoring.
        window.mode_row.set_selected(
            next(i for i, m in enumerate(MODES)
                 if m[1] is builder.BuildMode.EXPORT))
        pump()
        check(window._mode() is builder.BuildMode.EXPORT, "chosen for the check")
        window._forget_tasks_that_write_no_card()
        window._sync_visibility()
        pump()
        check(window._mode() is not builder.BuildMode.EXPORT,
              "a restored session does not open on Export")
        check(window.stack.get_visible_child_name() == "quick",
              f"it opens on the first screen: "
              f"{window.stack.get_visible_child_name()!r}")
        check(not window.back_button.get_visible(),
              "with no Back on it")

        window._go_back()
        pump()
        check(quick_page.get_visible(), "and Back brings the first screen back")
        check(window.stack.get_visible_child_name() == "quick",
              f"landing on it, not merely enabling it: "
              f"{window.stack.get_visible_child_name()!r}")
        check(not window.back_button.get_visible(),
              "with no Back on it once more")

        elsewhere = []
        for screen in ("basic", "prepared", "image", "default"):
            window._set_quick_screen(screen)
            pump()
            if window._quick_screen == screen:
                elsewhere.append((f"quick/{screen}",
                                  window.back_button.get_visible(),
                                  window.bottom_bar.get_visible()))
        window._set_quick_screen("choices")
        window._set_customising(True)
        pump()
        for name in ("source", "storage", "amiga", "packages", "options",
                     "target"):
            if window.stack.get_child_by_name(name) is None:
                continue
            window.stack.set_visible_child_name(name)
            pump()
            elsewhere.append((name, window.back_button.get_visible(),
                              window.bottom_bar.get_visible()))
        window._set_customising(False)
        window.mode_row.set_selected(
            next(i for i, m in enumerate(MODES)
                 if m[1] is builder.BuildMode.EXPORT))
        window._sync_visibility()
        pump()
        elsewhere.append(("export", window.back_button.get_visible(),
                          window.bottom_bar.get_visible()))

        check(len(elsewhere) >= 10,
              f"every other screen was actually visited: {len(elsewhere)}")
        without = [n for n, back, _bar in elsewhere if not back]
        check(not without, f"and every one of them has a Back: missing {without}")
        barless = [n for n, _back, bar in elsewhere if not bar]
        check(not barless, f"and a bar to put it in: missing {barless}")

        window._go_back()
        pump()

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
