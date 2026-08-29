"""Target machine profiles.

Almost everything about a PiStorm card is the same whatever Amiga it goes into:
the games, the demos, WHDLoad, the work partition.  What genuinely differs is
which PiStorm board is fitted, which Kickstart suits the machine, what the
chipset can display, and a handful of memory options that only exist on some
models.  Keeping that difference in one place means the rest of a build can be
identical everywhere.
"""
from __future__ import annotations

import dataclasses
import enum

from . import bootcfg


class Chipset(enum.Enum):
    OCS = "OCS"
    ECS = "ECS"
    AGA = "AGA"
    NONE = "none"           # a bare Raspberry Pi, with no Amiga around it

    @property
    def native_colours(self) -> str:
        return {
            Chipset.OCS: "32 colours (64 with EHB), HAM6",
            Chipset.ECS: "32 colours (64 with EHB), HAM6, productivity modes",
            Chipset.AGA: "256 colours, HAM8",
            Chipset.NONE: "no Amiga chipset",
        }[self]


class Display(enum.Enum):
    """How the machine is actually being looked at."""

    NATIVE = "native"           # the Amiga's own RGB/composite output
    RTG_HDMI = "rtg"            # the Pi's HDMI, using Emu68's RTG driver
    FRAMETHROWER = "framethrower"  # Amiga video captured into the Pi's HDMI

    @property
    def label(self) -> str:
        return {
            Display.NATIVE: "The Amiga's own video output",
            Display.RTG_HDMI: "The Pi's HDMI output (RTG)",
            Display.FRAMETHROWER: "Framethrower - Amiga video and RTG on HDMI",
        }[self]

    @property
    def uses_rtg(self) -> bool:
        return self in (Display.RTG_HDMI, Display.FRAMETHROWER)


@dataclasses.dataclass(frozen=True)
class Machine:
    key: str
    label: str
    chipset: Chipset
    board: str                       # an emu68.VARIANTS key
    board_label: str
    #  Kickstarts that suit this machine, best first, as (version, revision).
    kickstarts: tuple[tuple[int, int], ...]
    trapdoor_ram: bool = False       # the A500's 512K expansion at 0xC00000
    notes: str = ""

    @property
    def aga(self) -> bool:
        return self.chipset is Chipset.AGA


MACHINES: list[Machine] = [
    Machine("a500", "Amiga 500", Chipset.OCS, "pistorm", "PiStorm (classic)",
            ((40, 68), (40, 63), (37, 175)), trapdoor_ram=True,
            notes="The classic PiStorm replaces the 68000. The chipset is "
                  "untouched, so the Amiga's own video output stays OCS."),
    Machine("a500plus", "Amiga 500+", Chipset.ECS, "pistorm", "PiStorm (classic)",
            ((40, 68), (40, 63), (37, 175)), trapdoor_ram=True,
            notes="ECS chipset; otherwise identical to an A500 for our purposes."),
    Machine("a600", "Amiga 600", Chipset.ECS, "pistorm32lite", "PiStorm16",
            ((40, 68), (40, 63)),
            notes="PiStorm16 is the board for the A600 and uses a Compute "
                  "Module 4. It shares Emu68's build with the PiStorm32-lite."),
    Machine("a1000", "Amiga 1000", Chipset.OCS, "pistorm", "PiStorm (classic)",
            ((40, 68), (40, 63)),
            notes="OCS, and the machine has no Kickstart ROM of its own, so a "
                  "mapped Kickstart is essential."),
    Machine("a2000", "Amiga 2000", Chipset.ECS, "pistorm", "PiStorm (classic)",
            ((40, 68), (40, 63)),
            notes="Zorro II slots are present, so leave room for other cards "
                  "when setting the Zorro RAM size."),
    Machine("a1200", "Amiga 1200", Chipset.AGA, "pistorm32lite",
            "PiStorm32-lite", ((40, 68), (47, 111), (47, 96)),
            notes="AGA, and the only model here that can show 256-colour "
                  "native screen modes."),
    Machine("raspi", "Raspberry Pi on its own", Chipset.NONE, "raspi",
            "No PiStorm", ((40, 68),),
            notes="Emu68 with no Amiga hardware at all: no chipset, so RTG on "
                  "HDMI is the only display."),
]

MACHINES_BY_KEY = {m.key: m for m in MACHINES}


def boot_options(machine: Machine, display: Display,
                 hdmi: tuple[int | None, int | None] = (None, None),
                 trapdoor_to_chip: bool = False) -> bootcfg.BootOptions:
    """The Emu68 settings that follow from the hardware and the display."""
    options = bootcfg.BootOptions()

    #  Software written for OCS/ECS machines often busy-waits on the chipset,
    #  which a JIT runs straight past.
    options.chip_slowdown = machine.chipset in (Chipset.OCS, Chipset.ECS)
    #  Moving the vector base into fast RAM is quicker but, in Emu68's own
    #  words, hurts floppy-loaded games and demos badly - the wrong trade on
    #  a machine that exists to run them.
    options.vbr_move = False

    if machine.trapdoor_ram and trapdoor_to_chip:
        options.extra_cmdline = "move_slow_to_chip"
    if machine.chipset in (Chipset.OCS, Chipset.ECS):
        options.enable_slow_ram = True

    if display is Display.NATIVE:
        #  No RTG, so nothing should force an HDMI mode.
        options.hdmi_automatic = True
    else:
        group, mode = hdmi
        if group and mode:
            options.hdmi_group, options.hdmi_mode = group, mode
        else:
            options.hdmi_automatic = True
        options.vc4_mem = 64
    if display is Display.FRAMETHROWER:
        options.unicam = True
        options.unicam_smooth = True
    return options


def wants_rtg_drivers(machine: Machine, display: Display) -> bool:
    """Whether an RTG driver should be installed for this combination."""
    return display.uses_rtg


def advice(machine: Machine, display: Display) -> list[str]:
    """Plain warnings and reminders for this combination."""
    out = [machine.notes] if machine.notes else []
    if display is Display.NATIVE:
        out.append(
            f"Workbench will run on the {machine.chipset.value} chipset: "
            f"{machine.chipset.native_colours}. No RTG driver is installed.")
        if not machine.aga:
            out.append(
                "Do not choose a 256-colour screen mode - that needs AGA.")
        out.append(
            "A system image built for RTG (ClassicWB P96, PiMiga) can boot to "
            "a screen you cannot see on this setup; prefer a native one.")
    else:
        out.append(
            "Emu68's RTG driver draws on the Pi's HDMI output, not the Amiga's "
            "video port.")
        out.append(
            "Attach a monitor or splitter to the Pi's HDMI before powering on: "
            "the port is configured once at startup and the driver cannot "
            "change it later.")
    if display is Display.FRAMETHROWER:
        out.append(
            "Framethrower captures the Amiga's own video into the Pi so native "
            "screens and RTG share one HDMI display.")
    if machine.chipset is Chipset.NONE and display is Display.NATIVE:
        out.append(
            "With no Amiga attached there is no native video at all - use the "
            "Pi's HDMI.")
    return out


def kickstart_preference(machine: Machine) -> tuple[tuple[int, int], ...]:
    return machine.kickstarts
