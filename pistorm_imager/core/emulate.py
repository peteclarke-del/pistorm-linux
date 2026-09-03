"""Emulator settings derived from the machine the card was built for.

Testing a card in FS-UAE means describing the Amiga it is going into, and that
description already exists: the model, the chipset, the board and the trapdoor
choice are what the build itself is driven by.  Writing it a second time by
hand is how the two came apart.  The harness scripts used through one long
bisection had ``amiga_model = A1200`` (AGA, not the ECS A500 in question),
``fpu = 68040`` on a machine whose accelerator has no FPU at all, and
``accuracy = 0``, which runs a fast, inexact 68040 on which WHDLoad cannot
start a single game - so every test ran against a machine unlike the one being
built for, in three ways at once, and one of those cost hours of hunting a
defect in the imager that was a flag in the emulator.

So the settings come from ``machines.Machine`` and the ``BuildConfig``, and
nothing is typed twice.
"""
from __future__ import annotations

from pathlib import Path

from . import machines
from .machines import Chipset

#  Which FS-UAE model stands in for each of ours.  FS-UAE picks the chipset
#  from the model, so this is the chipset mapping as much as the model one.
#  An ECS A500 is a real board with a Super Denise fitted rather than one of
#  FS-UAE's presets: A500+ is the closest it has, and its 2 MB of chip RAM is
#  overridden below by what the trapdoor choice actually gives.
FSUAE_MODELS = {
    "a500": "A500",
    "a500ecs": "A500+",
    "a500plus": "A500+",
    "a600": "A600",
    "a1000": "A1000",
    "a2000": "A500+",      # ECS, and FS-UAE has no plain A2000 model
    "a1200": "A1200",
    "raspi": "A1200",      # nothing Amiga-side to emulate; a sane default
}

#  What a PiStorm presents, whatever board it is.  Emu68 gives a 68040 and
#  **no FPU**: a floating point instruction raises a line-F exception, which
#  the Amiga reports as guru 8000000B.  Giving the emulated machine an FPU
#  hides exactly the class of failure this project keeps meeting.
PISTORM_CPU = "68040"

#  A500-family boards take a 512K trapdoor expansion.  Emu68 can map it into
#  the chip range, which is what "move_slow_to_chip" does, and the machine
#  then has a megabyte of chip RAM instead of half of one.
BASE_CHIP_KB = 512
TRAPDOOR_CHIP_KB = 1024


def fsuae_model(machine: machines.Machine) -> str:
    """The FS-UAE model that matches this machine's chipset."""
    known = FSUAE_MODELS.get(machine.key)
    if known:
        return known
    #  A machine added later: fall back on the chipset, which is what the
    #  model is standing in for anyway.
    return {Chipset.AGA: "A1200", Chipset.ECS: "A500+"}.get(machine.chipset,
                                                            "A500")


def chip_memory_kb(machine: machines.Machine, trapdoor_to_chip: bool) -> int:
    """How much chip RAM the real machine will have, in KB."""
    if machine.trapdoor_ram and trapdoor_to_chip:
        return TRAPDOOR_CHIP_KB
    if machine.chipset is Chipset.AGA:
        return 2048
    return BASE_CHIP_KB


def fsuae_config(machine: machines.Machine, drive: str | Path,
                 kickstart: str | Path, *,
                 trapdoor_to_chip: bool = False,
                 fast_memory_kb: int = 8192,
                 window: tuple[int, int] = (1280, 1014),
                 extra: dict[str, str] | None = None) -> str:
    """An FS-UAE configuration for testing a card built for ``machine``.

    ``drive`` is the Amiga drive to attach - the whole ``0x76`` partition
    lifted out of a card image, so that every drive on it mounts and can be
    checked, rather than the bootable one on its own.
    """
    lines = [
        "[fs-uae]",
        f"# Written by the PiStorm imager for {machine.label}",
        f"# {machine.board_label}, which Emu68 presents as a {PISTORM_CPU}",
        f"amiga_model = {fsuae_model(machine)}",
        f"cpu = {PISTORM_CPU}",
        #  Emu68 provides no FPU, and a library built for a 68881 gurus with
        #  8000000B on the real machine.  Asked for here - but FS-UAE 3.0.3
        #  accepts the line silently and says nothing either way, so whether
        #  it takes effect is UNVERIFIED.  Assume the emulator may still be
        #  providing the 68040's internal FPU: floating point code can pass
        #  here and still bring the real machine down.
        "#  Emu68 provides no FPU. Unverified on FS-UAE 3.0.3 - floating",
        "#  point code may still run here and guru on the real machine.",
        "fpu = none",
        "#  accuracy = 0 runs a fast, inexact 68040 on which WHDLoad cannot",
        "#  start any game: every title gurus with a privilege violation in",
        "#  WHDLoad's own task, which reads exactly like a broken card and is",
        "#  not.  The same card runs clean here.",
        "accuracy = 1",
        f"kickstart_file = {Path(kickstart)}",
        f"hard_drive_0 = {Path(drive)}",
        f"chip_memory = {chip_memory_kb(machine, trapdoor_to_chip)}",
        f"fast_memory = {fast_memory_kb}",
        "fullscreen = 0",
        f"window_width = {window[0]}",
        f"window_height = {window[1]}",
        "automatic_input_grab = 0",
    ]
    for key, value in (extra or {}).items():
        lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"
