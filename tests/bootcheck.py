"""Boot a card this tool built, in FS-UAE, and see whether it gets to the end.

Not a test, and not a synthetic system: the files handed to the emulator are
the ones the build wrote onto the card, read back out of its Amiga volume and
laid out as a directory drive - which is how this project already runs an
AmigaOS binary under FS-UAE for the Boing Bag updater.

    python3 tests/bootcheck.py card.hdf          # needs a display

One line is added to the end of ``S:User-Startup``, after everything the build
put there, so that a boot which reaches the end says so. Everything above it
runs exactly as it was written. That is the point: several packages add lines
to that file, and one of them blocks until a network interface answers, so
"does the machine still get to Workbench" is a question worth asking of the
card rather than of the code.

It answers with the exit status, so it can be run from anywhere: 0 when the
boot reached the end, 1 when it did not.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pistorm_imager.core import amigaos, bbupdate, emulate, machines  # noqa: E402

#  The ROM the sample Workbench disks belong with.
ROM = ROOT / "samples" / "kickstart" / "KS ROM v3.1 (A1200) rev 40.68 (512k).rom"
MARKER = "booted-to-the-end"
TIMEOUT = 180


def extract(volume, into: Path) -> int:
    """Every file on the volume, laid out on the host as the Amiga has it."""
    count = 0
    for path, entry in volume.walk():
        target = into / path
        if entry.is_dir:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(volume.read_file(entry))
        count += 1
    return count


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    card = Path(argv[1])
    command = bbupdate.fsuae_command()
    if command is None:
        print("FS-UAE is not installed, so nothing can be booted")
        return 2
    if not ROM.is_file():
        print(f"no Kickstart ROM at {ROM}")
        return 2

    #  A confined FS-UAE sees neither /tmp nor the hidden parts of the home,
    #  so the tree goes where it can always read it.
    work = bbupdate.work_area() / "bootcheck"
    shutil.rmtree(work, ignore_errors=True)
    tree = work / "Workbench"
    tree.mkdir(parents=True)

    volume, label = amigaos.open_amiga_volume(card)
    print(f"reading {label} out of {card.name}")
    print(f"  {extract(volume, tree)} files")

    startup = tree / "S" / "User-Startup"
    body = startup.read_bytes() if startup.exists() else b""
    if body:
        print("--- S:User-Startup as the build wrote it ---")
        print(body.decode("latin-1").strip())
    startup.parent.mkdir(parents=True, exist_ok=True)
    startup.write_bytes(body + f'\nEcho >SYS:{MARKER} "reached the end"\n'
                        .encode("latin-1"))

    rom = work / "kickstart.rom"
    shutil.copy2(ROM, rom)
    config = work / "bootcheck.fs-uae"
    config.write_text(emulate.fsuae_config(
        machines.MACHINES_BY_KEY["a1200"], tree, rom,
        extra={"window_width": "640", "window_height": "512",
               "fullscreen": "0"}))

    marker = tree / MARKER
    print(f"booting with {command}")
    started = time.monotonic()
    process = subprocess.Popen([command, str(config)],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    reached = False
    try:
        while time.monotonic() - started < TIMEOUT:
            if marker.exists():
                reached = True
                break
            time.sleep(2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    if reached:
        print(f"booted to the end of S:User-Startup in "
              f"{time.monotonic() - started:.0f}s")
        return 0
    print(f"did not reach the end of S:User-Startup within {TIMEOUT}s")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
