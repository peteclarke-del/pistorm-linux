"""Adapting a prepared system on the card after it has been written.

Writing a finished image such as CaffeineOS copies raw sectors, so none of the
file-by-file compatibility work that a copied system gets ever happens to it -
which is right, because a system built for Emu68 already has the drivers it
needs. What it cannot know is which *screen* this particular machine is being
watched on.

CaffeineOS is the clear case. Its startup-sequence already branches on the
board it finds: under WinUAE it loads the UAEgfx monitor and applies
``ENVARC:Sys/screenmode.prefs.UAE``; on a PiStorm it loads emu68-VideoCore and
applies ``screenmode.prefs.PI``. Both lists include the Amiga's own Native
monitor, so a native screen is available either way. What the system does not
distinguish is a PiStorm with a monitor on the Pi's HDMI output from one with
nothing there but the Amiga's own 15 kHz video - and on the second, applying
the saved RTG mode opens Workbench where nobody can see it.

The fix is small and needs no rewriting of the system: blank the saved screen
mode so the ScreenMode command has nothing to apply, and the machine keeps the
native screen it started on. The user can then choose a mode in Prefs and save
it themselves. Blanking a file's data touches no metadata - the extents are
already allocated - so it is safe to do to a finished volume, which deleting a
file would not be.
"""
from __future__ import annotations

from . import pfs3, rdb
from .util import Progress

#  Saved screen-mode preferences that would put Workbench on an RTG screen.
#  CaffeineOS keeps one per board; a system with a single active file uses the
#  plain name.  Anything not present is simply skipped.
RTG_SCREENMODES = (
    "Prefs/Env-Archive/Sys/screenmode.prefs.PI",
    "Prefs/Env-Archive/Sys/screenmode.prefs",
    "Devs/Env-Archive/Sys/screenmode.prefs.PI",
    "Devs/Env-Archive/Sys/screenmode.prefs",
)


def adapt_display(handle, base: int, table: "rdb.Rdb", rtg: bool,
                  progress: Progress) -> int:
    """Make a written system open Workbench where it can be seen.

    Only ever removes a *saved* choice; it never installs one, because which
    mode suits a given monitor is not something this can know. Returns the
    number of preferences blanked.
    """
    if rtg:
        #  There is a screen on the Pi's HDMI output, which is what the saved
        #  mode selects.  Nothing to undo.
        return 0

    partition = next((p for p in table.partitions if p.bootable),
                     table.partitions[0] if table.partitions else None)
    if partition is None:
        return 0
    if partition.dostype not in (rdb.DOSTYPE_PFS3, rdb.DOSTYPE_PDS3):
        progress.log("  the boot drive is not PFS3; its screen mode was left "
                     "alone")
        return 0

    offset = partition.byte_offset(table.geometry, base)
    try:
        volume = pfs3.Pfs3Volume(handle, offset)
    except pfs3.Pfs3Error as error:
        progress.log(f"  could not read {partition.drive_name}: {error}")
        return 0

    cleared = 0
    for path in RTG_SCREENMODES:
        entry = volume.find(path)
        if entry is None or entry.is_dir or not entry.size:
            continue
        blanked = pfs3.clear_file_data(volume, path)
        if blanked:
            cleared += 1
            progress.log(f"  blanked {path} ({blanked} bytes): Workbench will "
                         f"open on the Amiga's own screen")
    if not cleared:
        progress.log("  no saved RTG screen mode found; nothing to change")
    return cleared
