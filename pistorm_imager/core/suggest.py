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


def _runs_on(choice: OsChoice, version: int) -> bool:
    """Whether ``choice`` runs on a Kickstart of this major version."""
    return version >= choice.kickstart_from and (
        choice.kickstart_to is None or version <= choice.kickstart_to)


def _ranked(found: list[OsChoice], machine: Machine,
            accelerator: Accelerator, cpu: Cpu,
            fitted: int | None = None) -> list[OsChoice]:
    """Best first.

    With a ROM chip that is known - anything but a PiStorm, whose Kickstart
    is a file - a release made for that ROM first, newest first: 3.9 on a
    3.1 chip, 3.2 on a 3.2 chip. One that only runs on it by loading its
    own modules over it - 3.2 on a 3.1 chip, the pairing with a documented
    crash - comes after.

    Otherwise, with a PiStorm or another accelerator, the newest release
    that runs: it has years of fixes the older ones never got, and the
    speed to carry them. On a stock 68000 the newest release whose
    Kickstart the machine itself takes: what it was built to run.
    """
    if fitted is not None:
        return sorted(found, key=lambda c: (c.best_kickstart == fitted,
                                            c.year), reverse=True)
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
         have_media: bool, cd_rom,
         fitted_rom: kickstart.RomInfo | None = None) -> Suggestion:
    """Everything that goes with installing ``choice`` on this machine."""
    soft = accelerator is Accelerator.PISTORM
    rom = (fitted_rom if fitted_rom is not None and not soft else
           _rom_for(choice, list(roms) + ([cd_rom] if cd_rom else []),
                    machine))
    reasons: list[str] = []
    if fitted_rom is not None and not soft:
        fitted = fitted_rom.version or 0
        if choice.best_kickstart == fitted:
            reasons.append(f"{choice.label} ({choice.year}) is the newest "
                           f"release made for the {fitted_rom.name} fitted "
                           f"in this machine, and it runs on the {cpu.label}.")
        else:
            reasons.append(
                f"{choice.label} runs on the {fitted_rom.name} fitted in this "
                f"machine only by loading its own Kickstart "
                f"{choice.best_kickstart} modules over it at every boot. Fit "
                f"a Kickstart {choice.best_kickstart} ROM for the sound "
                f"pairing.")
    elif _fast(machine, accelerator, cpu):
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
    if choice.best_kickstart != choice.kickstart_from \
            and (soft or fitted_rom is None):
        reasons.append(f"Boot it from its own Kickstart "
                       f"{choice.best_kickstart} ROM rather than an older "
                       f"one.")
    if fitted_rom is None and not soft:
        reasons.append("Say which Kickstart is fitted - the ROM on the "
                       "Machine step - and this is worked out for it: the "
                       "chip in the machine decides what can run.")
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
    if fitted_rom is not None and not soft:
        #  The chip is what it is: its pairing is said in the reasons, and
        #  is not something a file from anywhere can supply.
        pass
    elif not best_rom and not (_carries_its_roms(choice) and have_media):
        missing.append(f"a Kickstart {choice.best_kickstart} ROM for the "
                       f"{machine.label}"
                       + (" (it runs on the older one found, but this is "
                          "the sound pairing)" if rom is not None else ""))
    load = packages.suggested(machine, display, networking=networking,
                              pi=pi, cpu=cpu, emu68_tag=emu68_tag,
                              release=packages.os_release(choice.key),
                              emu68=accelerator is Accelerator.PISTORM)
    return Suggestion(choice, rom, reasons, missing, load)


def suggest(machine: Machine, accelerator: Accelerator, display: Display, *,
            card_cpu: Cpu | None = None, pi: Pi | None = None,
            emu68_tag: str | None = None, networking: bool = False,
            roms: list[kickstart.RomInfo] = (),
            adf_versions: list[str] = (),
            cd_release: str = "",
            cd_rom: kickstart.RomInfo | None = None,
            fitted_rom: kickstart.RomInfo | None = None
            ) -> Suggestion | None:
    """The AmigaOS that best suits this machine, and what goes with it.

    ``adf_versions`` are the floppy sets found, ``cd_release`` the release of
    the CD chosen (if one is), ``cd_rom`` the Kickstart that CD carries for
    this machine, and ``roms`` the Kickstart files found. Where the best
    needs something that is not to hand, ``instead`` is the best that can
    be installed with what is.
    """
    cpu = machine.cpu_fitted(accelerator, card_cpu)
    #  The Kickstart fitted, where it decides: a chip, not a PiStorm's file.
    fitted = (fitted_rom.version if fitted_rom is not None
              and fitted_rom.version
              and accelerator is not Accelerator.PISTORM else None)
    runs = [c for c in choices() if cpu.at_least(c.needs_cpu)
            and (not display.uses_rtg or c.best_kickstart >= 39)
            and (fitted is None or _runs_on(c, fitted))]
    if not runs:
        return None

    def here(choice: OsChoice) -> bool:
        return (choice.source == "cd" and choice.key == cd_release) or \
            (choice.source == "adf" and choice.key in set(adf_versions))

    common = dict(pi=pi, emu68_tag=emu68_tag, networking=networking,
                  roms=roms, cd_rom=cd_rom,
                  fitted_rom=fitted_rom if fitted is not None else None)
    ranked = _ranked(runs, machine, accelerator, cpu, fitted)
    best = _for(ranked[0], machine, accelerator, display, cpu,
                have_media=here(ranked[0]), **common)
    if best.missing:
        alt = next((c for c in ranked if here(c) and c != ranked[0]), None)
        if alt is not None:
            best.instead = _for(alt, machine, accelerator, display, cpu,
                                have_media=True, **common)
    return best
