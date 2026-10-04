"""Which AmigaOS suits a machine, and the software that goes with it.

Worked out from what is known rather than looked up: each release says what
it needs (a processor, a Kickstart) and when it came out, the machine says
what it has, and the media say what can actually be installed. The answer
names the release, the Kickstart to boot it from, what is still missing, and
the software load the Software page would suggest for that release.
"""
from __future__ import annotations

import dataclasses

from . import amigacd, amigaos, kickstart, packages
from .machines import Accelerator, Chipset, Cpu, Display, Machine, Pi


@dataclasses.dataclass(frozen=True)
class OsChoice:
    """One AmigaOS this tool can install, from its CD or its floppies."""

    key: str            # "3.2", "3.1" - the release as the build names it
    label: str
    source: str         # "cd" or "adf"
    year: int
    needs_cpu: Cpu
    #  The Kickstart it runs on, oldest and newest major version; no newest
    #  means any newer one.
    kickstart_from: int
    kickstart_to: int | None
    #  The Kickstart it is best on.
    best_kickstart: int


def choices() -> list[OsChoice]:
    """Every release there is to choose from."""
    out = [OsChoice(r.key, r.label, "cd", r.year, r.needs_cpu,
                    r.kickstart_from, r.kickstart_to,
                    r.best_kickstart or r.kickstart_from)
           for r in amigacd.RELEASES]
    out += [OsChoice(f.version, f"Workbench {f.version}", "adf", f.year,
                     Cpu.M68000, f.kickstart, f.kickstart, f.kickstart)
            for f in amigaos.FLOPPY_RELEASES]
    return out


@dataclasses.dataclass
class Suggestion:
    choice: OsChoice
    rom: kickstart.RomInfo | None
    reasons: list[str]
    missing: list[str]
    packages: list[str]
    #  The best that can be installed with what is to hand, where the
    #  suggestion itself needs something that is not.
    instead: "Suggestion | None" = None

    @property
    def release(self) -> tuple[int, ...] | None:
        return packages.os_release(self.choice.key)


def _rom_for(choice: OsChoice, roms: list[kickstart.RomInfo],
             machine: Machine) -> kickstart.RomInfo | None:
    """The Kickstart to boot ``choice`` from, of those found.

    The release's own best first, then any it runs on; for each, one built
    for this machine's chipset first - an AGA ROM for an A1200 - and the
    newest revision.
    """
    aga = machine.chipset is Chipset.AGA

    def runs(rom: kickstart.RomInfo) -> bool:
        version = rom.version or 0
        return version >= choice.kickstart_from and (
            choice.kickstart_to is None or version <= choice.kickstart_to)

    usable = [r for r in roms if r.usable and r.version and runs(r)]
    if not usable:
        return None
    return max(usable, key=lambda r: (
        r.version == choice.best_kickstart,
        r.aga is aga, r.aga is None, r.revision or 0))


def _fast(machine: Machine, accelerator: Accelerator, cpu: Cpu) -> bool:
    """Whether the machine has more than a stock 68000's speed to give."""
    return accelerator is not Accelerator.STOCK or cpu.at_least(Cpu.M68020)


def _ranked(found: list[OsChoice], machine: Machine,
            accelerator: Accelerator, cpu: Cpu) -> list[OsChoice]:
    """Best first.

    With a PiStorm or another accelerator, the newest release that runs:
    it has years of fixes the older ones never got, and the speed to carry
    them. On a stock 68000 the newest release whose Kickstart the machine
    itself was sold with: what it was built to run, not asked to stretch.
    """
    if _fast(machine, accelerator, cpu):
        return sorted(found, key=lambda c: c.year, reverse=True)
    own = {major for major, _rev in machine.kickstarts}
    return sorted(found, key=lambda c: (c.best_kickstart in own, c.year),
                  reverse=True)


def _carries_its_roms(choice: OsChoice) -> bool:
    """Whether the release's own disc has Kickstarts on it to boot from."""
    release = amigacd.RELEASES_BY_KEY.get(choice.key)
    return choice.source == "cd" and bool(release and release.roms)


def _for(choice: OsChoice, machine: Machine, accelerator: Accelerator,
         display: Display, cpu: Cpu, *, pi, emu68_tag, networking, roms,
         have_media: bool, cd_rom) -> Suggestion:
    """Everything that goes with installing ``choice`` on this machine."""
    rom = _rom_for(choice, list(roms) + ([cd_rom] if cd_rom else []), machine)
    reasons: list[str] = []
    if _fast(machine, accelerator, cpu):
        provided = ("Emu68 provides" if accelerator is Accelerator.PISTORM
                    else "it has")
        reasons.append(f"{choice.label} ({choice.year}) is the newest "
                       f"release this machine runs, and the {cpu.label} "
                       f"{provided} carries it easily.")
    else:
        reasons.append(f"{choice.label} is the newest release on a "
                       f"Kickstart the {machine.label} takes as its own; "
                       f"newer ones cost a stock {cpu.label} speed and "
                       f"memory it has not got to spare.")
    if choice.best_kickstart != choice.kickstart_from:
        reasons.append(f"Boot it from its own Kickstart "
                       f"{choice.best_kickstart} ROM rather than an older "
                       f"one.")
    if display.uses_rtg:
        reasons.append("It runs the RTG screen through Picasso96.")
    if accelerator is Accelerator.PISTORM:
        reasons.append("On a PiStorm the Kickstart is the file on the SD "
                       "card, not the chip in the Amiga, so no ROM has to "
                       "be fitted.")
    missing: list[str] = []
    if not have_media:
        missing.append(f"the {choice.label} CD image" if choice.source == "cd"
                       else f"the {choice.label} floppy images")
    best_rom = rom is not None and rom.version == choice.best_kickstart
    if not best_rom and not (_carries_its_roms(choice) and have_media):
        missing.append(f"a Kickstart {choice.best_kickstart} ROM for the "
                       f"{machine.label}"
                       + (" (it runs on the older one found, but this is "
                          "the sound pairing)" if rom is not None else ""))
    load = packages.suggested(machine, display, networking=networking,
                              pi=pi, cpu=cpu, emu68_tag=emu68_tag,
                              release=packages.os_release(choice.key))
    return Suggestion(choice, rom, reasons, missing, load)


def suggest(machine: Machine, accelerator: Accelerator, display: Display, *,
            card_cpu: Cpu | None = None, pi: Pi | None = None,
            emu68_tag: str | None = None, networking: bool = False,
            roms: list[kickstart.RomInfo] = (),
            adf_versions: list[str] = (),
            cd_release: str = "",
            cd_rom: kickstart.RomInfo | None = None) -> Suggestion | None:
    """The AmigaOS that best suits this machine, and what goes with it.

    ``adf_versions`` are the floppy sets found, ``cd_release`` the release of
    the CD chosen (if one is), ``cd_rom`` the Kickstart that CD carries for
    this machine, and ``roms`` the Kickstart files found. Where the best
    needs something that is not to hand, ``instead`` is the best that can
    be installed with what is.
    """
    cpu = machine.cpu_fitted(accelerator, card_cpu)
    runs = [c for c in choices() if cpu.at_least(c.needs_cpu)
            and (not display.uses_rtg or c.best_kickstart >= 39)]
    if not runs:
        return None

    def here(choice: OsChoice) -> bool:
        return (choice.source == "cd" and choice.key == cd_release) or \
            (choice.source == "adf" and choice.key in set(adf_versions))

    common = dict(pi=pi, emu68_tag=emu68_tag, networking=networking,
                  roms=roms, cd_rom=cd_rom)
    ranked = _ranked(runs, machine, accelerator, cpu)
    best = _for(ranked[0], machine, accelerator, display, cpu,
                have_media=here(ranked[0]), **common)
    if best.missing:
        alt = next((c for c in ranked if here(c) and c != ranked[0]), None)
        if alt is not None:
            best.instead = _for(alt, machine, accelerator, display, cpu,
                                have_media=True, **common)
    return best
