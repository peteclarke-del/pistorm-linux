"""Identifying Amiga Kickstart ROM images.

Emu68 maps a Kickstart from the boot partition via the ``initramfs`` line in
config.txt, and it needs a *plain, big-endian, unencrypted* ROM.  Users' ROM
collections routinely contain Cloanto-encrypted files and byte-swapped dumps, so
rather than matching file names we look inside: the ROM header carries a version
and revision, which tells us both the Kickstart version and (for 3.1) whether it
is the AGA build Emu68 wants.
"""
from __future__ import annotations

import dataclasses
import hashlib
import struct
from pathlib import Path

CLOANTO_MAGIC = b"AMIROMTYPE1"

#  ROM identification: (version, revision) -> (human name, is_aga_a1200)
KNOWN_ROMS: dict[tuple[int, int], tuple[str, bool]] = {
    (34, 5): ("Kickstart 1.3 (34.5)", False),
    (37, 175): ("Kickstart 2.04 (37.175)", False),
    (37, 210): ("Kickstart 2.05 (37.210)", False),
    (39, 106): ("Kickstart 3.0 A1200/A4000 (39.106)", True),
    (40, 63): ("Kickstart 3.1 A500/A600/A2000 (40.63)", False),
    (40, 68): ("Kickstart 3.1 A1200/A4000 (40.68)", True),
    (40, 70): ("Kickstart 3.1 A4000T (40.70)", True),
    (45, 57): ("Kickstart 3.1.4 (45.57)", True),
    (46, 143): ("Kickstart 3.1.4 A1200 (46.143)", True),
    (47, 96): ("Kickstart 3.2 A1200 (47.96)", True),
    (47, 102): ("Kickstart 3.2.1 A1200 (47.102)", True),
    (47, 111): ("Kickstart 3.2.2 A1200 (47.111)", True),
    (47, 115): ("Kickstart 3.2.3 A1200 (47.115)", True),
}

VALID_SIZES = {256 * 1024, 512 * 1024, 1024 * 1024}


@dataclasses.dataclass
class RomInfo:
    path: Path
    size: int
    version: int | None
    revision: int | None
    name: str
    aga: bool
    encrypted: bool
    byte_swapped: bool
    sha1: str
    usable: bool
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} - {self.path.name}"


def _unswap(data: bytes) -> bytes:
    out = bytearray(data)
    out[0::2], out[1::2] = data[1::2], data[0::2]
    return bytes(out)


def decrypt_cloanto(data: bytes, key: bytes) -> bytes:
    """Decrypt an ``AMIROMTYPE1`` ROM with the contents of ``rom.key``.

    Cloanto's scheme is a repeating XOR over everything after the 11-byte magic.
    """
    body = data[len(CLOANTO_MAGIC):]
    if not key:
        raise ValueError("rom.key is empty")
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(body))


def _header_version(data: bytes) -> tuple[int, int] | None:
    """Read (version, revision) from a Kickstart ROM header, if it looks valid."""
    if len(data) < 16:
        return None
    if data[0:2] not in (b"\x11\x11", b"\x11\x14", b"\x11\x16"):
        return None
    version, revision = struct.unpack_from(">HH", data, 12)
    if version == 0 or version > 100:
        return None
    return version, revision


def identify(path: str | Path, key_file: str | Path | None = None) -> RomInfo:
    """Inspect a candidate Kickstart file."""
    path = Path(path)
    raw = path.read_bytes()
    sha1 = hashlib.sha1(raw).hexdigest()
    size = len(raw)
    encrypted = raw.startswith(CLOANTO_MAGIC)
    note = ""
    data = raw

    if encrypted:
        key_path = Path(key_file) if key_file else path.with_name("rom.key")
        if key_path.exists():
            try:
                data = decrypt_cloanto(raw, key_path.read_bytes())
                note = f"decrypted with {key_path.name}"
            except Exception as error:  # noqa: BLE001 - report, do not crash a scan
                return RomInfo(path, size, None, None, "Encrypted ROM (decryption failed)",
                               False, True, False, sha1, False, str(error))
        else:
            return RomInfo(path, size, None, None,
                           "Encrypted Cloanto ROM (rom.key not found)",
                           False, True, False, sha1, False,
                           "Place rom.key beside the ROM, or point the tool at it")

    byte_swapped = False
    header = _header_version(data)
    if header is None:
        swapped = _unswap(data)
        header = _header_version(swapped)
        if header is not None:
            byte_swapped = True
            data = swapped
            note = (note + "; " if note else "") + "byte-swapped dump, will be corrected"

    if header is None:
        return RomInfo(path, size, None, None, "Not a Kickstart ROM", False,
                       encrypted, False, sha1, False,
                       "No valid ROM header found")

    version, revision = header
    name, aga = KNOWN_ROMS.get((version, revision),
                               (f"Kickstart {version}.{revision}", version >= 39))
    usable = len(data) in VALID_SIZES
    if not usable:
        note = (note + "; " if note else "") + f"unusual ROM size ({size} bytes)"
    return RomInfo(path, len(data), version, revision, name, aga, encrypted,
                   byte_swapped, sha1, usable, note)


def prepare(info: RomInfo, key_file: str | Path | None = None) -> bytes:
    """Return the plain ROM bytes to write to the boot partition as kick.rom."""
    raw = info.path.read_bytes()
    if raw.startswith(CLOANTO_MAGIC):
        key_path = Path(key_file) if key_file else info.path.with_name("rom.key")
        if not key_path.exists():
            raise RuntimeError(
                f"{info.path.name} is encrypted and rom.key was not found next to it"
            )
        raw = decrypt_cloanto(raw, key_path.read_bytes())
    if info.byte_swapped:
        raw = _unswap(raw)
    return raw


def scan(folder: str | Path, key_file: str | Path | None = None) -> list[RomInfo]:
    """Find every plausible ROM under ``folder`` (recursively)."""
    folder = Path(folder)
    results: list[RomInfo] = []
    if not folder.is_dir():
        return results
    for candidate in sorted(folder.rglob("*")):
        if not candidate.is_file():
            continue
        if candidate.name.lower() == "rom.key":
            continue
        if candidate.stat().st_size not in VALID_SIZES | {
                size + len(CLOANTO_MAGIC) for size in VALID_SIZES}:
            continue
        try:
            info = identify(candidate, key_file)
        except OSError:
            continue
        if info.version is not None or info.encrypted:
            results.append(info)
    return results
