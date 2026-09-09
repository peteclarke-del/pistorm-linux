"""Applying a locked BoingBag by running its own Updater under FS-UAE.

BoingBags 1 and 2 for AmigaOS 3.9 keep every system file they fix inside
``AmigaOS-Update``, a ZIP whose entries are all encrypted; the password is
inside Haage & Partner's ``Updater``, and their installer simply runs
``C/Updater AmigaOS-Update <target>``.

So this project runs that command rather than trying to open the archive.  The
system tree has already been staged as a directory on Linux, FS-UAE can mount a
directory as an Amiga drive, and ``Updater`` therefore writes its results
straight back into the staging tree - which the build then copies onto the card
exactly as if the files had always been there.

Nothing here defeats a protection measure: the vendor's own tool is given the
vendor's own archive and does the job it was written for.  When FS-UAE is not
installed the caller falls back to the files that are in the clear and says
which fixes were left out.

**The disc has to be there too.**  ``Updater`` will not apply anything until it
has seen the CD it is an update for: it opens a requester reading "Please insert
volume AmigaOS3.9 in any drive" and waits.  What it wants is a *volume of that
name*, not a CD device, so the disc is unpacked into a directory and FS-UAE is
told to label that drive ``AmigaOS3.9``.  No CD-ROM emulation is involved, and
no CD file system has to be running on the Amiga - which would otherwise be a
chicken and egg, because CacheCDFS is itself on the disc.

This was found by watching the run rather than by reasoning about it.  Reading
the pack's Installer script does not show it, because the check is inside the
``Updater`` binary; the emulator sat at a requester until somebody looked.

**How the run ends.**  An Amiga cannot quit its emulator, so the Amiga side
writes a marker file when it has finished and this side watches the staging
tree for it.  That is the same trick the PFS3 tests use.  A run that never
writes one is stopped on a timeout and reported as a failure rather than left
to hang a build.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import boingbag, emulate, iso9660, machines
from .util import Progress

#  The snap publishes itself under a different name, and a plain "fs-uae" is
#  not on the path when it is installed that way.
FSUAE_COMMANDS = ("fs-uae", "fsuae.fs-uae")

#  Long enough for a 3.9 boot and two update passes on a slow machine, short
#  enough that a build does not appear to have hung.
DEFAULT_TIMEOUT = 300

MARKER = "BoingBag-Applied"
#  Where the pack is mounted on the Amiga side.
PACK_DRIVE = "DH1"
#  And the copy being updated, which is deliberately not the one booted.
TARGET_LABEL = "Updating"


def fsuae_command() -> str | None:
    for name in FSUAE_COMMANDS:
        found = shutil.which(name)
        if found:
            return found
    return None


def available() -> bool:
    return fsuae_command() is not None


def is_confined(command: str | None = None) -> bool:
    """Whether FS-UAE is installed as a snap, and so cannot see everywhere."""
    command = command or fsuae_command() or ""
    try:
        resolved = str(Path(command).resolve())
    except OSError:
        resolved = command
    return resolved.startswith("/snap/") or "/snap/" in command


def work_area(preferred: Path | None = None) -> Path:
    """A directory the emulator can actually read.

    A snap is confined to the user's home *and not to the hidden parts of it*,
    so the obvious places are all unreachable: ``/tmp`` is invisible, and so is
    ``~/.cache``, which is where this project keeps everything else.  A run
    staged there configures no drives at all and simply sits at an insert-disk
    screen until the timeout - which looks exactly like a failed update and is
    not one.

    So when FS-UAE is confined the work goes in the snap's own writable area,
    which it can always read, and is cleaned up afterwards.  When it is a
    normal installation any temporary directory will do.
    """
    if not is_confined():
        return preferred or Path(tempfile.mkdtemp(prefix="pistorm-emulator-"))
    #  SNAP_USER_COMMON: the snap's own writable area, which it can always
    #  read, and which is not hidden from it the way ~/.cache is.
    common = Path.home() / "snap" / "fsuae" / "common" / "pistorm-imager"
    common.mkdir(parents=True, exist_ok=True)
    return common


def _startup_lines(bag: boingbag.Bag) -> list[str]:
    """What the Amiga runs once it has booted.

    ``Updater`` is taken from the pack rather than from the system, because a
    3.9 system that has not been updated yet does not have one.
    """
    lines = [
        "; Written by the PiStorm imager to apply a BoingBag, and removed",
        "; again once it has been applied.",
        "Assign BB: " + PACK_DRIVE + ":",
    ]
    for payload in bag.locked_payloads:
        #  Updating the *other* copy, not the one this booted from.
        lines.append(f'BB:C/Updater BB:{payload} "{TARGET_LABEL}:"')
    #  The marker is the signal to this side that the work is done.  It is
    #  written last, so its presence means every payload has been through.
    lines.append(f'Echo >SYS:{MARKER} "applied"')
    return lines


def _reachable(path: Path, work_dir: Path) -> bool:
    """Whether a confined emulator could read this path.

    Only the visible part of the user's home is reachable from a snap, so a
    hidden directory anywhere under it is as invisible as /tmp is.
    """
    try:
        relative = Path(path).resolve().relative_to(Path.home().resolve())
    except ValueError:
        return False
    return not any(part.startswith(".") for part in relative.parts)


def _clear_targets(staged: Path, archive_root: Path, bag: boingbag.Bag,
                   keep: Path, progress: Progress) -> int:
    """Move aside the files a locked payload is going to replace.

    Moved rather than deleted, and put back if the run does not finish.  An
    update that fails half way would otherwise leave the system *missing* the
    files it was meant to improve - a worse card than the one that was there
    before, and one that would still look like a successful build.
    """
    moved = 0
    for name in boingbag.locked_entries(archive_root, bag):
        target = staged / name
        if not target.is_file():
            continue
        saved = keep / name
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(saved))
        moved += 1
    if moved:
        progress.log(f"  {moved} files moved aside for {bag.label} to replace")
    return moved


def _restore_targets(staged: Path, keep: Path) -> int:
    """Put back everything ``_clear_targets`` moved that was not replaced."""
    if not keep.is_dir():
        return 0
    restored = 0
    for saved in sorted(keep.rglob("*")):
        if saved.is_dir():
            continue
        target = staged / saved.relative_to(keep)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(saved), str(target))
            restored += 1
    shutil.rmtree(keep, ignore_errors=True)
    return restored


def _install_hook(staged: Path, bag: boingbag.Bag) -> Path:
    """Add the update to the staged system's startup, keeping the original."""
    startup = staged / "S" / "User-Startup"
    backup = staged / "S" / "User-Startup.before-boingbag"
    startup.parent.mkdir(parents=True, exist_ok=True)
    if startup.exists():
        shutil.copy2(startup, backup)
        original = startup.read_bytes()
    else:
        original = b""
        backup.write_bytes(b"")
    body = original
    if body and not body.endswith(b"\n"):
        body += b"\n"
    body += ("\n".join(_startup_lines(bag)) + "\n").encode("latin-1")
    startup.write_bytes(body)
    return backup


def _remove_hook(staged: Path) -> None:
    """Put the system's own startup back, and take the marker off the drive.

    Both matter to what ends up on the card: a User-Startup with an Updater
    call in it would try to run again on the real machine, against a pack that
    is not there, and the marker is this project's own scaffolding rather than
    part of AmigaOS.
    """
    startup = staged / "S" / "User-Startup"
    backup = staged / "S" / "User-Startup.before-boingbag"
    if backup.exists():
        if backup.stat().st_size:
            shutil.copy2(backup, startup)
        elif startup.exists():
            startup.unlink()
        backup.unlink()
    marker = staged / MARKER
    if marker.exists():
        marker.unlink()
    #  FS-UAE writes a metadata sidecar beside each file on a directory drive.
    for sidecar in staged.rglob("*.uaem"):
        sidecar.unlink()


def unpack_disc(iso_path: str | Path, into: Path,
                progress: Progress | None = None) -> str:
    """Lay the CD out as a directory, and say what volume it should be called.

    ``Updater`` checks for the disc by volume name before it will do anything,
    so the whole disc goes out - not only the trees an install copies - and the
    drive is labelled with the name the disc itself carries.
    """
    into.mkdir(parents=True, exist_ok=True)
    with iso9660.IsoImage.open(iso_path) as iso:
        volume = iso.volume_name
        count = 0
        for relative, entry in iso.walk():
            if entry.is_dir:
                (into / relative).mkdir(parents=True, exist_ok=True)
            else:
                iso.extract(entry, into / relative)
                count += 1
    if progress is not None:
        progress.log(f"  the disc laid out as volume {volume} for the "
                     f"update's own media check ({count} files)")
    return volume


def apply_locked(bag: boingbag.Bag, archive_root: Path, staged: Path,
                 machine: machines.Machine, kickstart: str | Path,
                 progress: Progress, *,
                 timeout: int = DEFAULT_TIMEOUT,
                 work_dir: Path | None = None,
                 disc_image: str | Path | None = None) -> bool:
    """Run this pack's Updater against the staged tree.  True if it worked."""
    command = fsuae_command()
    if command is None:
        progress.log(f"  {bag.label}: FS-UAE is not installed, so its own "
                     f"Updater cannot be run")
        return False
    if not Path(kickstart).is_file():
        progress.log(f"  {bag.label}: no Kickstart ROM to boot the emulator "
                     f"with, so its Updater cannot be run")
        return False
    if not bag.locked_payloads:
        return True

    staged = Path(staged)
    work_dir = Path(work_dir) if work_dir is not None else work_area()
    work_dir.mkdir(parents=True, exist_ok=True)
    config_file = work_dir / "updater.fs-uae"

    #  Everything the emulator has to read must be somewhere it can reach.  A
    #  confined FS-UAE sees neither /tmp nor the hidden parts of the home, so
    #  the tree, the pack and the ROM are brought to it and the results copied
    #  back.  Where it is not confined this costs a move that is not needed,
    #  so it is only done when it is.
    #  Two copies of the system, and this is the whole trick.
    #
    #  The files a locked payload writes have to be moved out of its way,
    #  because XAD will not write over one that is already there.  But those
    #  files are the operating system: C/SetPatch is among them, and the
    #  machine runs it out of the Startup-Sequence long before it reaches the
    #  Updater.  Clearing the volume the emulator boots from therefore stops
    #  it booting - "C:SetPatch: Unknown command", and the run gets no
    #  further.
    #
    #  So the machine boots one copy, which is left whole, and updates the
    #  other, which is cleared.  Only the second is kept.
    boot_on = work_dir / "Boot"
    target_on = work_dir / "Target"
    pack_on = work_dir / "Pack"
    rom_on = work_dir / "kickstart.rom"
    for directory in (boot_on, target_on, pack_on):
        shutil.rmtree(directory, ignore_errors=True)
    shutil.copytree(staged, boot_on)
    shutil.copytree(staged, target_on)
    shutil.copytree(archive_root, pack_on)
    shutil.copy2(kickstart, rom_on)

    #  The disc, as a volume of its own name, because Updater refuses to run
    #  without seeing it.  Laid out beside the run rather than in place, so a
    #  read-only source stays read-only.
    disc_label = ""
    disc_here = None
    if disc_image is not None:
        disc_here = work_dir / "Disc"
        if not (disc_here.is_dir() and any(disc_here.iterdir())):
            disc_label = unpack_disc(disc_image, disc_here, progress)
        else:
            with iso9660.IsoImage.open(disc_image) as iso:
                disc_label = iso.volume_name

    #  Take the files the payload is about to write out of the way first.
    #  XAD, which is what Updater depacks with, refuses to write over a file
    #  that is already there - "Failed to depack the file C/IPrefs!
    #  XAD-Error: file already exists" - and every file it carries is one the
    #  CD has just installed, so every single one collides.
    #
    #  Which files those are is known without opening anything: a ZIP's
    #  directory lists its entries in the clear even when their contents are
    #  encrypted.  This is a staging tree, so removing them costs nothing - if
    #  the run fails the tree is rebuilt from the disc.
    kept_aside = work_dir / "replaced"
    shutil.rmtree(kept_aside, ignore_errors=True)
    _clear_targets(target_on, pack_on, bag, kept_aside, progress)

    progress.step(f"Applying {bag.label} with its own Updater under FS-UAE")
    backup = _install_hook(boot_on, bag)
    try:
        config = emulate.fsuae_config(
            machine, boot_on, rom_on,
            extra={
                #  The pack itself, as a second drive, so Updater and the
                #  payload are both reachable from the Amiga side.
                "hard_drive_1": str(pack_on),
                #  The copy being updated, which is not the one booted.
                "hard_drive_3": str(target_on),
                "hard_drive_3_label": TARGET_LABEL,
                #  Nothing to look at and nobody to watch it.
                "fullscreen": "0",
                "window_width": "640",
                "window_height": "512",
                **({"hard_drive_2": str(disc_here),
                    "hard_drive_2_label": disc_label,
                    "hard_drive_2_read_only": "1"} if disc_here else {}),
            })
        config_file.write_text(config)
        marker = boot_on / MARKER
        if marker.exists():
            marker.unlink()

        process = subprocess.Popen(
            [command, str(config_file)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        applied = False
        try:
            while time.monotonic() < deadline:
                if marker.exists():
                    applied = True
                    break
                if process.poll() is not None:
                    #  The emulator stopped on its own; the marker decides
                    #  whether that was after the work or instead of it.
                    applied = marker.exists()
                    break
                time.sleep(1.0)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)

        #  Whatever the update did not write, put back.  After a good run
        #  this restores nothing, because every file moved aside has been
        #  replaced; after a bad one it is what keeps the system whole.
        returned = _restore_targets(target_on, kept_aside)
        if returned:
            progress.log(f"  {returned} files the update did not replace were "
                         f"put back as the CD installed them")
        if applied:
            #  The updated copy becomes the staged system.  The booted copy is
            #  thrown away: it carries the startup hook and whatever the boot
            #  wrote, and none of that belongs on the card.
            _remove_hook(target_on)
            shutil.rmtree(staged)
            shutil.copytree(target_on, staged)
        if applied:
            progress.log(f"  {bag.label}: applied by its own Updater")
        else:
            progress.log(f"  {bag.label}: the emulator did not finish the "
                         f"update within {timeout} seconds")
        return applied
    finally:
        _restore_targets(target_on, kept_aside)
        _remove_hook(staged)
        for leftover in ("Boot", "Target", "Pack", "kickstart.rom"):
            leaving = work_dir / leftover
            if leaving.is_dir():
                shutil.rmtree(leaving, ignore_errors=True)
            elif leaving.exists():
                leaving.unlink()


def apply_or_report(bag: boingbag.Bag, archive_root: Path, staged: Path,
                    machine: machines.Machine, kickstart: str | Path,
                    progress: Progress, *, use_emulator: bool = True,
                    timeout: int = DEFAULT_TIMEOUT,
                    disc_image: str | Path | None = None) -> bool:
    """Apply a locked pack, or say file by file what could not be applied.

    The fallback names the files rather than counting them, because "some
    fixes were skipped" is not something anybody can act on and the list is
    what tells you whether the thing you are chasing is in it.
    """
    if not boingbag.is_locked(archive_root, bag):
        return True
    if use_emulator and apply_locked(bag, archive_root, staged, machine,
                                     kickstart, progress, timeout=timeout,
                                     disc_image=disc_image):
        return True
    boingbag.report_skipped(bag, archive_root, progress)
    return False
