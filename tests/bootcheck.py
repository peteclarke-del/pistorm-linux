"""Boot a card this tool built, in FS-UAE, and see whether it gets to the end.

Not a test, and not a synthetic system: the files handed to the emulator are
the ones the build wrote onto the card, read back out of its Amiga volume and
laid out as a directory drive - which is how this project already runs an
AmigaOS binary under FS-UAE for the Boing Bag updater.

    python3 tests/bootcheck.py card.hdf          # needs a display
    python3 tests/bootcheck.py card.hdf 3.2.rom 68020 a500

The Kickstart, the processor and the machine can be named after the card. The processor
matters for AmigaOS 3.2, whose boot script stops a 68040 that has no
68040.library: Emu68 carries one in its kernel and an emulator does not, so a
3.2 card is booted here as a 68020, which that check lets through.

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
        #  The emulator reads these back, so a script keeps its script bit.
        amigaos.write_sidecar(target, entry.protect & 0xFF, entry.days,
                              entry.mins, entry.ticks, entry.comment)
        count += 1
    return count


def main(argv: list[str]) -> int:
    if not 2 <= len(argv) <= 5:
        print(__doc__)
        return 2
    card = Path(argv[1])
    rom_file = Path(argv[2]) if len(argv) > 2 else ROM
    processor = argv[3] if len(argv) > 3 else ""
    machine_key = argv[4] if len(argv) > 4 else "a1200"
    command = bbupdate.fsuae_command()
    if command is None:
        print("FS-UAE is not installed, so nothing can be booted")
        return 2
    if not rom_file.is_file():
        print(f"no Kickstart ROM at {rom_file}")
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

    #  Under whatever spelling the card has it: the emulator's drive is a
    #  Linux directory, where "User-startup" and "User-Startup" are two files
    #  and AmigaOS runs the one that was already there.
    startup = next((path for path in (tree / "S").glob("*")
                    if path.name.lower() == "user-startup"),
                   tree / "S" / "User-Startup")
    body = startup.read_bytes() if startup.exists() else b""
    if body:
        print("--- S:User-Startup as the build wrote it ---")
        print(body.decode("latin-1").strip())
    startup.parent.mkdir(parents=True, exist_ok=True)
    startup.write_bytes(body + f'\nEcho >SYS:{MARKER} "reached the end"\n'
                        .encode("latin-1"))

    rom = work / "kickstart.rom"
    shutil.copy2(rom_file, rom)
    config = work / "bootcheck.fs-uae"
    settings = emulate.fsuae_config(
        machines.MACHINES_BY_KEY[machine_key], tree, rom,
        extra={"window_width": "640", "window_height": "512",
               "fullscreen": "0"})
    if processor:
        settings = "\n".join(
            f"cpu = {processor}" if line.startswith("cpu = ") else line
            for line in settings.splitlines()
            if not line.startswith("fpu = ")) + "\n"
    config.write_text(settings)

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
