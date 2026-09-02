"""Prepared Amiga systems a card can be based on.

Several people distribute a whole, finished AmigaOS installation as an image.
Pointing the tool at one is a perfectly good way to build a card - far quicker
than installing Workbench from six floppies and adding everything by hand -
but they are not interchangeable, and the differences matter enough that the
tool should say which one it is looking at rather than treat every image as an
anonymous blob.

None of this is shipped here; each is downloaded from its author. What this
module holds is how to recognise one and what is worth knowing about it before
a card is committed to it.

The most consequential difference is where Workbench appears. A system built
around RTG puts its desktop on the Pi's HDMI output and nothing else, so on a
machine being watched on its own 15 kHz video there is a Workbench nobody can
see - which is a thing to be told before writing the card, not after.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

GIB = 1024 ** 3


@dataclasses.dataclass(frozen=True)
class Distribution:
    key: str
    label: str
    home: str
    summary: str
    #  Volume labels that identify it.  Matched case-insensitively against the
    #  Amiga drives the image holds, which is the one thing that survives the
    #  author repartitioning between releases.
    volumes: tuple[str, ...] = ()
    #  Already built for Emu68 and the PiStorm, so its graphics setup is the
    #  right one already and must not be substituted the way an emulator's is.
    emu68_native: bool = False
    #  Workbench lives on an RTG screen and nowhere else.
    rtg_only: bool = False
    minimum_card: int = 0
    advice: tuple[str, ...] = ()


CATALOGUE: list[Distribution] = [
    Distribution(
        "caffeineos", "CaffeineOS",
        "https://caffeineos.neocities.org/",
        "A finished AmigaOS 3.9 installation built for Emu68 and the PiStorm, "
        "using Dopus Magellan as its Workbench replacement.",
        volumes=("CaffeineOS",),
        emu68_native=True,
        rtg_only=True,
        minimum_card=64 * GIB,
        advice=(
            "Its Workbench opens on an RTG screen only, so on a machine "
            "watched on the Amiga's own video output there will be a desktop "
            "nobody can see. Use the Pi's HDMI output for Workbench.",
            "It brings its own Kickstart - a custom AmigaOS 3.9 ROM on the "
            "boot partition - and its own Emu68 kernel and command line.",
            "64 GB card or larger.",
        ),
    ),
    Distribution(
        "pimiga", "PiMiga",
        "https://www.youtube.com/@ChrisEdwardsRetro",
        "A Raspberry Pi system running the Amiberry emulator. Its Amiga "
        "drives are ordinary folders inside its Linux root partition, not "
        "Amiga partitions, so it is used through the PiMiga folder source "
        "rather than as an image.",
        volumes=(),
        advice=(
            "Mount its Linux root partition and point the PiMiga folder "
            "source at the disks/ folder inside it.",
        ),
    ),
]

CATALOGUE_BY_KEY = {d.key: d for d in CATALOGUE}


def identify(path: str | Path) -> Distribution | None:
    """Recognise a prepared system from the drives an image holds."""
    from . import builder                       # late: builder is heavy
    labels = {drive.volume.strip().lower()
              for drive in builder.list_drives(path) if drive.volume}
    if not labels:
        return None
    for distribution in CATALOGUE:
        for wanted in distribution.volumes:
            if wanted.lower() in labels:
                return distribution
    return None


def describe(distribution: Distribution, card_size: int = 0) -> list[str]:
    """What to tell the user about a system they have chosen."""
    lines = [distribution.summary]
    lines += list(distribution.advice)
    if (distribution.minimum_card and card_size
            and card_size < distribution.minimum_card):
        lines.append(
            f"This card is smaller than the {distribution.minimum_card // GIB} GB "
            f"{distribution.label} expects.")
    return lines
