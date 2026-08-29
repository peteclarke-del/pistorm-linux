"""Checking an Amiga hard disk image for PiStorm/Emu68 compatibility.

Not every ``.hdf`` in circulation works on a PiStorm.  Most were built for
WinUAE, where the emulator is forgiving about geometry and about drivers that
the host happens to provide anyway.  Emu68 hands the 0x76 partition to AmigaOS
as a real drive, so the Rigid Disk Block has to be internally consistent and
has to carry every file system handler its partitions ask for.

Each check yields a :class:`Finding`.  Those marked ``fixable`` are metadata
repairs - they rewrite fields in the RDB and never touch partition contents, so
applying them cannot lose data.  Anything that would require moving or
reformatting a partition is reported but never done automatically.
"""
from __future__ import annotations

import dataclasses

from . import rdb
from .util import human_size

ERROR = "error"
WARNING = "warning"
INFO = "info"

#  Kickstart 3.x can mount these without a handler in the RDB.
ROM_DOSTYPES = {
    rdb.DOSTYPE_OFS, rdb.DOSTYPE_FFS, rdb.DOSTYPE_OFS_INTL,
    rdb.DOSTYPE_FFS_INTL, rdb.DOSTYPE_OFS_DC, rdb.DOSTYPE_FFS_DC,
}
DIRCACHE_DOSTYPES = {rdb.DOSTYPE_OFS_DC, rdb.DOSTYPE_FFS_DC}

#  The classic cause of silent corruption on real hardware: a MaxTransfer of
#  0xFFFFFF asks the driver for transfers it cannot do in one go.
SAFE_MAX_TRANSFER = 0x0001FE00
SAFE_MASK = 0x7FFFFFFE

#  Handlers that are the same binary under a different DosType.  PFS3 and PDS3
#  differ only in whether the handler talks to the device directly, so one
#  donor image satisfies a partition asking for either - the FSHD just has to
#  be labelled with the DosType the partition actually requests.
INTERCHANGEABLE = [{rdb.DOSTYPE_PFS3, rdb.DOSTYPE_PDS3}]


def compatible_dostypes(dostype: int) -> set[int]:
    for family in INTERCHANGEABLE:
        if dostype in family:
            return set(family)
    return {dostype}


@dataclasses.dataclass
class Finding:
    code: str
    severity: str
    message: str
    fixable: bool = False
    partition: str = ""

    def __str__(self) -> str:
        where = f" [{self.partition}]" if self.partition else ""
        tag = {ERROR: "ERROR", WARNING: "warning", INFO: "note"}[self.severity]
        fix = " (fixable)" if self.fixable else ""
        return f"{tag}{where}: {self.message}{fix}"


def analyse(table: rdb.Rdb, capacity_bytes: int) -> list[Finding]:
    """Inspect an RDB that will live in ``capacity_bytes`` of space."""
    findings: list[Finding] = []
    geometry = table.geometry
    cyl_blocks = geometry.cyl_blocks
    capacity_blocks = capacity_bytes // rdb.BLOCK

    if geometry.block_size != rdb.BLOCK:
        findings.append(Finding(
            "block_size", ERROR,
            f"the RDB uses {geometry.block_size}-byte blocks; Emu68's SD driver "
            f"presents 512-byte blocks, so this image cannot be used as is"))

    if geometry.heads <= 0 or geometry.sectors <= 0:
        findings.append(Finding(
            "geometry", ERROR,
            f"nonsensical drive geometry ({geometry.heads} heads x "
            f"{geometry.sectors} sectors)", fixable=True))
        return findings          # nothing else can be judged meaningfully

    if table.cylinders * cyl_blocks > capacity_blocks:
        findings.append(Finding(
            "drive_too_big", WARNING,
            f"the RDB claims {human_size(table.cylinders * cyl_blocks * rdb.BLOCK)} "
            f"but only {human_size(capacity_bytes)} is available",
            fixable=True))

    if not table.partitions:
        findings.append(Finding("no_partitions", ERROR,
                                "the RDB contains no partitions"))

    seen_names: dict[str, int] = {}
    ordered = sorted(table.partitions, key=lambda p: p.low_cyl)
    for first, second in zip(ordered, ordered[1:]):
        if second.low_cyl <= first.high_cyl:
            findings.append(Finding(
                "overlap", ERROR,
                f"{first.drive_name} (cyl {first.low_cyl}-{first.high_cyl}) and "
                f"{second.drive_name} (cyl {second.low_cyl}-{second.high_cyl}) "
                f"overlap; this will destroy data and cannot be fixed "
                f"automatically", partition=second.drive_name))

    embedded = {fs.dostype for fs in table.filesystems}
    for part in table.partitions:
        name = part.drive_name
        seen_names[name] = seen_names.get(name, 0) + 1

        if part.high_cyl < part.low_cyl:
            findings.append(Finding(
                "inverted", ERROR,
                f"partition ends (cyl {part.high_cyl}) before it starts "
                f"(cyl {part.low_cyl})", partition=name))
        if (part.high_cyl + 1) * cyl_blocks > capacity_blocks:
            findings.append(Finding(
                "past_end", ERROR,
                f"extends to {human_size((part.high_cyl + 1) * cyl_blocks * rdb.BLOCK)}, "
                f"beyond the {human_size(capacity_bytes)} available; use a larger "
                f"card or a smaller boot partition", partition=name))
        if part.low_cyl == 0:
            findings.append(Finding(
                "over_rdb", ERROR,
                "starts at cylinder 0, on top of the RDB itself", partition=name))

        if part.dostype not in ROM_DOSTYPES and part.dostype not in embedded:
            findings.append(Finding(
                "missing_handler", ERROR,
                f"is {rdb.dostype_name(part.dostype)}, which Kickstart cannot mount "
                f"on its own, and no matching handler is embedded in the RDB",
                fixable=True, partition=name))
        if part.dostype in DIRCACHE_DOSTYPES:
            findings.append(Finding(
                "dircache", WARNING,
                "uses a directory-cache file system, which is known to corrupt "
                "on large volumes", partition=name))

        if part.max_transfer > SAFE_MAX_TRANSFER:
            findings.append(Finding(
                "max_transfer", WARNING,
                f"MaxTransfer is 0x{part.max_transfer:X}; values above "
                f"0x{SAFE_MAX_TRANSFER:X} are a classic cause of silent data "
                f"corruption on real hardware", fixable=True, partition=name))
        if part.mask & 1 or part.mask == 0:
            findings.append(Finding(
                "mask", WARNING,
                f"transfer Mask is 0x{part.mask:08X}, which allows odd addresses",
                fixable=True, partition=name))
        if part.sectors_per_block != 1:
            findings.append(Finding(
                "sectors_per_block", WARNING,
                f"SectorsPerBlock is {part.sectors_per_block}; it should be 1",
                fixable=True, partition=name))
        if part.reserved_blocks < 1:
            findings.append(Finding(
                "reserved", WARNING,
                "reserves no blocks for the file system root", fixable=True,
                partition=name))
        if part.num_buffers <= 0:
            findings.append(Finding(
                "buffers", WARNING, "requests no file system buffers",
                fixable=True, partition=name))

    for name, count in seen_names.items():
        if count > 1:
            findings.append(Finding(
                "duplicate_name", WARNING,
                f"{count} partitions are all called {name}; only the first will "
                f"mount", fixable=True, partition=name))

    if not any(p.bootable for p in table.partitions):
        findings.append(Finding(
            "not_bootable", WARNING,
            "no partition is marked bootable, so the Amiga will not start from "
            "this drive", fixable=True))

    return findings


def _free_name(used: set[str]) -> str:
    for index in range(10):
        candidate = f"DH{index}"
        if candidate not in used:
            return candidate
    for index in range(100):
        candidate = f"HD{index}"
        if candidate not in used:
            return candidate
    raise ValueError("no free device name")


def repair(table: rdb.Rdb, capacity_bytes: int,
           donors: list[rdb.FileSystem] | None = None) -> list[str]:
    """Apply every safe metadata fix. Returns a description of what changed."""
    actions: list[str] = []
    geometry = table.geometry

    if geometry.heads <= 0 or geometry.sectors <= 0:
        table.geometry = rdb.Geometry()
        geometry = table.geometry
        actions.append(f"set the drive geometry to {geometry.heads} heads x "
                       f"{geometry.sectors} sectors")

    cyl_blocks = geometry.cyl_blocks
    capacity_cylinders = (capacity_bytes // rdb.BLOCK) // cyl_blocks
    if table.cylinders > capacity_cylinders:
        highest = max((p.high_cyl for p in table.partitions), default=0)
        if highest < capacity_cylinders:
            actions.append(f"reduced the drive from {table.cylinders} to "
                           f"{capacity_cylinders} cylinders to match the space "
                           f"available")
            table.cylinders = capacity_cylinders

    embedded = {fs.dostype for fs in table.filesystems}
    needed = {p.dostype for p in table.partitions
              if p.dostype not in ROM_DOSTYPES and p.dostype not in embedded}
    for want in sorted(needed):
        #  Prefer an exact match, then an interchangeable one relabelled to the
        #  DosType this partition asks for.
        exact = next((d for d in donors or [] if d.dostype == want), None)
        family = compatible_dostypes(want)
        near = next((d for d in donors or [] if d.dostype in family), None)
        candidate = exact or near
        if candidate is None:
            continue
        handler = dataclasses.replace(candidate, dostype=want)
        table.filesystems.append(handler)
        embedded.add(want)
        origin = "" if exact else (f" (relabelled from "
                                   f"{rdb.dostype_name(candidate.dostype)})")
        actions.append(f"embedded a {rdb.dostype_name(want)} handler "
                       f"({len(handler.seglist)} bytes) in the RDB{origin}")

    used_names: set[str] = set()
    for part in table.partitions:
        name = part.drive_name
        if part.max_transfer > SAFE_MAX_TRANSFER:
            part.max_transfer = SAFE_MAX_TRANSFER
            actions.append(f"{name}: MaxTransfer lowered to 0x{SAFE_MAX_TRANSFER:X}")
        if part.mask & 1 or part.mask == 0:
            part.mask = SAFE_MASK
            actions.append(f"{name}: transfer Mask set to 0x{SAFE_MASK:08X}")
        if part.sectors_per_block != 1:
            part.sectors_per_block = 1
            actions.append(f"{name}: SectorsPerBlock set to 1")
        if part.reserved_blocks < 1:
            part.reserved_blocks = 2
            actions.append(f"{name}: reserved blocks set to 2")
        if part.num_buffers <= 0:
            part.num_buffers = 30
            actions.append(f"{name}: file system buffers set to 30")
        if name in used_names:
            new_name = _free_name(used_names | {p.drive_name for p in table.partitions})
            actions.append(f"{name}: renamed to {new_name} to avoid a clash")
            part.drive_name = new_name
            name = new_name
        used_names.add(name)

    if table.partitions and not any(p.bootable for p in table.partitions):
        first = min(table.partitions, key=lambda p: p.low_cyl)
        first.bootable = True
        first.boot_priority = 0
        actions.append(f"marked {first.drive_name} bootable")

    return actions


def summarise(findings: list[Finding]) -> str:
    if not findings:
        return "No compatibility problems found."
    errors = sum(1 for f in findings if f.severity == ERROR)
    warnings = sum(1 for f in findings if f.severity == WARNING)
    fixable = sum(1 for f in findings if f.fixable)
    parts = []
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    if warnings:
        parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    text = ", ".join(parts) or f"{len(findings)} notes"
    return f"{text}; {fixable} can be fixed automatically"


#  Problems that make the drive structurally unusable or unsafe to write.
#  A missing file system handler is deliberately not among them: the drive is
#  intact, the user simply has to supply the handler (or add it from HDToolBox
#  on the Amiga), so it is reported loudly but never aborts a build the user
#  asked for.
FATAL_CODES = {
    "block_size", "geometry", "no_partitions", "overlap", "past_end",
    "inverted", "over_rdb",
}


def blocking(findings: list[Finding], after_repair: bool = False) -> list[Finding]:
    """Problems that stop the image being written at all.

    Before repair, a ``fixable`` problem is not blocking because we are about to
    fix it.  Afterwards it is - "fixable" only ever meant that a repair exists,
    never that one succeeded.
    """
    return [f for f in findings
            if f.severity == ERROR and f.code in FATAL_CODES
            and (after_repair or not f.fixable)]


def unresolved(findings: list[Finding]) -> list[Finding]:
    """Errors that remain after repair but do not stop the write."""
    return [f for f in findings
            if f.severity == ERROR and f.code not in FATAL_CODES]
