"""AHI's settings, written the way its own Prefs program writes them.

AHI starts with no audio mode chosen until somebody runs AHI Prefs and saves
one, and until then everything that plays through it - AmigaAMP, the AUDIO:
handler, games that use it - either says so or is silent. The card is given
the settings AHI Prefs would have saved, with the mode read from the driver's
own list in DEVS:AudioModes rather than an ID written in here.

The layouts are AHI's ``devices/ahi.h``: an IFF ``FORM PREF`` holding a
``PRHD`` header, one ``AHIG`` of global settings and an ``AHIU`` per unit.
"""
from __future__ import annotations

import dataclasses
import struct
from pathlib import Path

TAG_USER = 0x80000000
AHI_TAGBASE_R = TAG_USER | 0x8000
AHIDB_AUDIO_ID = TAG_USER + 100
AHIDB_PANNING = TAG_USER + 104
AHIDB_STEREO = TAG_USER + 105
AHIDB_NAME = AHI_TAGBASE_R + 109

#  The units AHI Prefs saves: the four device units and the music unit.
AHI_NO_UNIT = 255
UNITS = (0, 1, 2, 3, AHI_NO_UNIT)

#  AHI Prefs' own defaults for a unit it has not been told about
#  (support.c in AHI's source): one channel, 44.1 kHz, which the driver
#  brings down to what the hardware can do, full output, no monitoring.
CHANNELS = 1
FREQUENCY = 44100
UNITY = 0x10000                          # 1.0 in AHI's 16.16 fixed point

#  And its global defaults: no debugging, surround and echo allowed, 90% of
#  the processor at most, no clipping, scaled at 0 dB.
MAX_CPU = (90 << 16) // 100
AHI_SCALE_FIXED_0_DB = 2


@dataclasses.dataclass(frozen=True)
class AudioMode:
    id: int
    name: str
    stereo: bool
    panning: bool


def audio_modes(data: bytes) -> list[AudioMode]:
    """The modes a DEVS:AudioModes file lists, in its order.

    Each ``AUDM`` chunk is a tag list; strings are offsets into the chunk.
    """
    if data[:4] != b"FORM" or data[8:12] != b"AHIM":
        raise ValueError("not an AHI audio modes file")
    modes = []
    pos = 12
    while pos + 8 <= len(data):
        chunk = data[pos:pos + 4]
        size = struct.unpack_from(">I", data, pos + 4)[0]
        body = data[pos + 8:pos + 8 + size]
        if chunk == b"AUDM":
            tags: dict[int, int] = {}
            for at in range(0, len(body) - 7, 8):
                tag, value = struct.unpack_from(">II", body, at)
                if tag == 0:
                    break
                tags[tag] = value
            name = ""
            if AHIDB_NAME in tags and tags[AHIDB_NAME] < len(body):
                start = tags[AHIDB_NAME]
                end = body.find(b"\0", start)
                name = body[start:end if end >= 0 else None].decode("latin-1")
            if AHIDB_AUDIO_ID in tags:
                modes.append(AudioMode(tags[AHIDB_AUDIO_ID], name,
                                       bool(tags.get(AHIDB_STEREO)),
                                       bool(tags.get(AHIDB_PANNING))))
        pos += 8 + size + (size & 1)
    return modes


def stereo_plus_plus(modes: list[AudioMode]) -> AudioMode | None:
    """The first stereo mode with panning - what AHI calls stereo++.

    Players such as AmigaAMP ask for one and refuse plain stereo; the
    driver lists its best-quality modes first, so the first is the one.
    """
    return next((m for m in modes if m.stereo and m.panning), None)


def _chunk(name: bytes, body: bytes) -> bytes:
    return name + struct.pack(">I", len(body)) + body + b"\0" * (len(body) & 1)


def prefs(mode: int) -> bytes:
    """ENVARC:Sys/ahi.prefs with every unit set to ``mode``."""
    header = struct.pack(">BBI", 0, 0, 0)
    globals_ = struct.pack(">HhhhihHiH", 0, 0, 0, 0, MAX_CPU, 0, 0, 0,
                           AHI_SCALE_FIXED_0_DB)
    body = b"PREF" + _chunk(b"PRHD", header) + _chunk(b"AHIG", globals_)
    for unit in UNITS:
        body += _chunk(b"AHIU", struct.pack(
            ">BBHIIiiiII", unit, 0, CHANNELS, mode, FREQUENCY, 0, UNITY,
            UNITY, 0, 0))
    return _chunk(b"FORM", body)


def prefs_from(modes_file: Path) -> bytes:
    """The settings for the driver whose modes file this is."""
    chosen = stereo_plus_plus(audio_modes(modes_file.read_bytes()))
    if chosen is None:
        raise ValueError(f"{modes_file.name} lists no stereo++ mode")
    return prefs(chosen.id)
