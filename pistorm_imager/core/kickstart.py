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
import re
import struct
from collections.abc import Iterable
from pathlib import Path

CLOANTO_MAGIC = b"AMIROMTYPE1"

#  ROM identification: (version, revision) -> (human name, is_aga_a1200).
#  From 3.1.4 on, one version.revision is built for every model, so the pair
#  says the release and not the machine: those are None, and the model is
#  found by ``_models``. 3.2's 47.96 used to be called the A1200's here, and
#  the A500 build was then offered as the AGA ROM Emu68 wants.
KNOWN_ROMS: dict[tuple[int, int], tuple[str, bool | None]] = {
    (34, 5): ("Kickstart 1.3 (34.5)", False),
    (37, 175): ("Kickstart 2.04 (37.175)", False),
    (37, 210): ("Kickstart 2.05 (37.210)", False),
    (39, 106): ("Kickstart 3.0 A1200/A4000 (39.106)", True),
    (40, 63): ("Kickstart 3.1 A500/A600/A2000 (40.63)", False),
    (40, 68): ("Kickstart 3.1 A1200/A4000 (40.68)", True),
    (40, 70): ("Kickstart 3.1 A4000T (40.70)", True),
    (45, 57): ("Kickstart 3.1.4 (45.57)", None),
    (46, 143): ("Kickstart 3.1.4 (46.143)", None),
    (47, 96): ("Kickstart 3.2 (47.96)", None),
    (47, 102): ("Kickstart 3.2.1 (47.102)", None),
    (47, 111): ("Kickstart 3.2.2 (47.111)", None),
    (47, 115): ("Kickstart 3.2.3 (47.115)", None),
}

#  Builds whose model is known by their contents, as SHA-1 of the plain ROM:
#  the AmigaOS 3.2 CD's own, named there by the machines each is for.
ROM_MODELS: dict[str, tuple[str, ...]] = {
    "5b2982876fec2166673be447643881262c84090e": ("A1200",),
    "b88e364daf23c9c9920e548b0d3d944e65b1031d":
        ("A500", "A600", "A1000", "A2000", "CDTV"),
}

#  The machines a ROM's file can name, as Hyperion's own files do -
#  kicka1200.rom, kickCDTVa1000a500a2000a600.rom - and the AGA ones.
MODEL_NAMES = re.compile(r"(?i)(a4000t|a4000|a3000|a2000|a1200|a1000|a600|"
                         r"a500|cd32|cdtv)")
AGA_MODELS = {"A1200", "A4000", "A4000T", "CD32"}


def _models(data: bytes, path: Path) -> tuple[str, ...]:
    """The machines a ROM is built for: by its contents, else its name."""
    known = ROM_MODELS.get(hashlib.sha1(data).hexdigest())
    if known:
        return known
    named = []
    for found in MODEL_NAMES.findall(path.stem):
        model = found.upper()
        if model not in named:
            named.append(model)
    return tuple(named)

VALID_SIZES = {256 * 1024, 512 * 1024, 1024 * 1024}


@dataclasses.dataclass
class RomInfo:
    path: Path
    size: int
    version: int | None
    revision: int | None
    name: str
    #  None where nothing says which machine it was built for.
    aga: bool | None
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

    if header is None and encrypted:
        #  Decryption cannot fail, only give the wrong bytes: a rom.key from
        #  another set of ROMs turns this one into noise, which used to be
        #  reported as no Kickstart at all.
        return RomInfo(path, size, None, None,
                       "Encrypted ROM (this rom.key does not fit it)", False,
                       True, False, sha1, False,
                       f"Decrypting it with {note.split(' with ')[-1]} gives "
                       f"no Kickstart: that key is for another set of ROMs. "
                       f"Use the rom.key that came with this one")
    if header is None:
        return RomInfo(path, size, None, None, "Not a Kickstart ROM", False,
                       encrypted, False, sha1, False,
                       "No valid ROM header found")

    version, revision = header
    name, aga = KNOWN_ROMS.get((version, revision),
                               (f"Kickstart {version}.{revision}",
                                None if version >= 45 else version >= 39))
    if aga is None:
        models = _models(data, path)
        if models:
            aga = any(m in AGA_MODELS for m in models)
            release, _, numbers = name.rpartition(" (")
            name = f"{release} {'/'.join(models)} ({numbers}"
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


#  WHDLoad's relocation tables - the .RTB files beside each image in
#  Devs:Kickstarts - begin with the checksum their ROM stores 24 bytes from
#  its end. So the tables name the images WHDLoad can use: a ROM whose stored
#  checksum one of them begins with is that table's Kickstart, and goes on
#  the card under its name. Read from the tables rather than written here,
#  which held five hashes and so missed 3.0 and 2.04 that WHDLoad also takes.
TABLE_SUFFIX = ".RTB"


def stored_checksum(data: bytes) -> int | None:
    """The checksum a ROM image carries for itself, if it adds up.

    Exec's own check: every longword summed with the carry wrapped round
    comes to 0xFFFFFFFF. A ROM that does not is damaged or patched, and
    WHDLoad's tables are for the untouched one.
    """
    if len(data) < 24 or len(data) % 4:
        return None
    total = 0
    for (word,) in struct.iter_unpack(">I", data):
        total += word
        if total > 0xFFFFFFFF:
            total = (total & 0xFFFFFFFF) + 1
    if total != 0xFFFFFFFF:
        return None
    return struct.unpack_from(">I", data, len(data) - 24)[0]


def relocation_tables(paths: Iterable[str | Path]) -> dict[int, str]:
    """The checksum each relocation table is for, and the image name it wants.

    ``paths`` may be tables or drawers holding them.
    """
    out: dict[int, str] = {}
    for given in paths:
        given = Path(given)
        found = ([given] if given.is_file()
                 else sorted(given.rglob("*")) if given.is_dir() else [])
        for table in found:
            if not table.name.upper().endswith(TABLE_SUFFIX) \
                    or not table.is_file():
                continue
            head = table.read_bytes()[:4]
            if len(head) == 4:
                out.setdefault(struct.unpack(">I", head)[0],
                               table.name[:-len(TABLE_SUFFIX)])
    return out


def whdload_images(folder: str | Path, tables: dict[int, str],
                   key_file: str | Path | None = None
                   ) -> list[tuple[str, bytes, RomInfo]]:
    """Every ROM in ``folder`` WHDLoad can use, as (its name, image, source).

    ``tables`` is ``relocation_tables``' answer: the images there are tables
    for. Each ROM is decrypted and un-swapped first, so the card needs no
    rom.key. A 256K Kickstart is often kept doubled to fill 512K; the half is
    what is compared, and what WHDLoad wants.
    """
    found: dict[str, tuple[bytes, RomInfo]] = {}
    for info in scan(folder, key_file):
        try:
            data = prepare(info, key_file)
        except (RuntimeError, OSError):
            continue
        half = len(data) // 2
        if len(data) == 512 * 1024 and data[:half] == data[half:]:
            data = data[:half]
        checksum = stored_checksum(data)
        name = tables.get(checksum) if checksum is not None else None
        if name is not None and name not in found:
            found[name] = (data, info)
    return [(name, data, info) for name, (data, info) in sorted(found.items())]
