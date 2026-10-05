"""Redraw docs/os-suggestions.md from what core/suggest.py answers.

    python3 tests/os_suggestions.py

Every supported machine, with no accelerator, an accelerator of each
processor and a PiStorm - and, where the Kickstart is a chip, with a 3.1 and
a 3.2 one fitted. Every release's media is assumed to hand, and the
Kickstart files are the ones in samples/kickstart.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pistorm_imager import __version__  # noqa: E402
from pistorm_imager.core import amigaos, kickstart, machines, suggest  # noqa: E402
from pistorm_imager.core.machines import Accelerator, Chipset, Cpu, Display  # noqa: E402

ROMS = kickstart.scan(ROOT / "samples" / "kickstart")
ADFS = [f.version for f in amigaos.FLOPPY_RELEASES]
CDS = [c.key for c in suggest.choices() if c.source == "cd"]


def short(label: str) -> str:
    return label.replace("Workbench ", "WB ").replace("AmigaOS ", "OS ")


def chip(machine, version: int):
    """The Kickstart of this version built for this machine's chipset."""
    aga = machine.chipset is Chipset.AGA
    found = [r for r in ROMS if r.version == version and r.usable]
    return next((r for r in found if r.aga is aga), found[0] if found else None)


def row(machine, label, accelerator, card, fitted):
    best = None
    for cd in CDS:
        found = suggest.suggest(machine, accelerator, Display.NATIVE,
                                card_cpu=card, roms=ROMS, adf_versions=ADFS,
                                cd_release=cd, fitted_rom=fitted)
        if found is not None and (best is None or not found.missing):
            best = found
    cpu = machine.cpu_fitted(accelerator, card)
    fitted_version = fitted.version if fitted is not None else None
    runs = [c for c in suggest.choices() if cpu.at_least(c.needs_cpu)
            and (fitted_version is None or suggest._runs_on(c, fitted_version))]
    ranked = suggest._ranked(runs, machine, accelerator, cpu, fitted_version)
    rest = ", ".join(short(c.label) for c in ranked if c != best.choice)[:60]
    where = ("a file on the SD card" if accelerator is Accelerator.PISTORM
             else f"{fitted.version}.{fitted.revision} chip")
    return (f"| {machine.label} | {label} | {where} | **{best.choice.label}** "
            f"| {rest or '-'} |")


lines = []
for machine in machines.MACHINES:
    if machine.key == "raspi":
        lines.append(row(machine, "Emu68 (68040)", Accelerator.PISTORM, None,
                         None))
        continue
    combos = [(f"None - stock {machine.stock_cpu.value}", Accelerator.STOCK,
               None)]
    combos += [(f"Accelerator, {c.value}", Accelerator.ACCELERATOR, c)
               for c in (Cpu.M68020, Cpu.M68030, Cpu.M68040, Cpu.M68060)]
    for label, accelerator, card in combos:
        for version in (40, 47):
            lines.append(row(machine, label, accelerator, card,
                             chip(machine, version)))
    lines.append(row(machine, "PiStorm (Emu68, 68040)", Accelerator.PISTORM,
                     None, None))

DOC = f"""# Which AmigaOS the app suggests

The **Suggested for this machine** row on the System step, worked out by
`pistorm_imager/core/suggest.py`. This table is what that code answers
(version {__version__}) for every machine the app supports: with no accelerator,
with an accelerator of each processor, and with a PiStorm. Redraw it with
`python3 tests/os_suggestions.py`.

**Assumed for every row:** every release's media is to hand - the AmigaOS 3.2,
3.5 and 3.9 CDs and the Workbench 1.3, 2.0, 2.1, 3.0 and 3.1 floppies - and the
Kickstart files are the ones in `samples/kickstart`. With media missing the
suggestion is the same, says what is missing, and offers the best of what is
there as well. The screen makes no difference: an RTG screen only rules out
releases older than 3.0, and those never come first anyway.

## How it ranks

- **The Kickstart decides, where it is a chip.** Without a PiStorm, the ROM
  chosen on the Machine step is the one fitted in the machine. Only releases
  that run on it are suggested, and one **made for** it comes first, newest
  first: 3.9 on a 3.1 chip with a 68020, 3.2 on a 3.2 chip. One that only runs
  on it by loading its own modules over it at every boot - 3.2 on a 3.1 chip,
  the pairing with a documented crash - comes after, and says so.
- **On a PiStorm the Kickstart is a file**, so any release's own ROM can be
  had: the newest release that runs, on its own ROM.
- **No ROM given, without a PiStorm:** the newest release that runs with an
  accelerator, the newest on a Kickstart the machine takes as its own without
  one - and the suggestion asks for the ROM, since the chip decides.
- **The processor:** 3.5 and 3.9 need a 68020; 3.2 and the floppy releases run
  on a 68000.

## The matrix

"Kickstart" is the chip fitted, or the PiStorm's file. "Then" is the rest of
the ranking, best first.

| Machine | Accelerator | Kickstart | Suggested | Then |
| --- | --- | --- | --- | --- |
""" + "\n".join(lines) + "\n"

(ROOT / "docs" / "os-suggestions.md").write_text(DOC)
print(f"{len(lines)} rows written to docs/os-suggestions.md")
