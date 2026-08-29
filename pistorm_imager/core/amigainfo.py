"""Minimal reader/writer for Amiga Workbench ``.info`` icon files.

Only what is needed to retarget a Picasso96 monitor icon: read and rewrite the
tool types.  Picasso96 chooses its graphics board from the ``BOARDTYPE`` tool
type of the icon in ``DEVS:Monitors``, so converting a system from one RTG card
to another means editing that string.

Layout (from ``workbench/workbench.h``)::

    DiskObject      78 bytes, including a 44-byte embedded Gadget
    DrawerData      56 bytes, when do_DrawerData is set
    GadgetRender    Image header + planes, when gg_GadgetRender is set
    SelectRender    Image header + planes, when gg_SelectRender is set
    DefaultTool     ULONG length + NUL terminated string
    ToolTypes       ULONG (count+1)*4, then that many length+string entries
"""
from __future__ import annotations

import struct

MAGIC = 0xE310
DISKOBJECT_SIZE = 78
DRAWERDATA_SIZE = 56
IMAGE_HEADER_SIZE = 20
GADGET_OFFSET = 4


class InfoError(RuntimeError):
    pass


def _image_size(data: bytes, offset: int) -> int:
    """Bytes occupied by an Image header plus its bitplanes."""
    if offset + IMAGE_HEADER_SIZE > len(data):
        raise InfoError("truncated image header")
    width, height, depth = struct.unpack_from(">HHH", data, offset + 4)
    planes = ((width + 15) >> 4) * 2 * height * depth
    return IMAGE_HEADER_SIZE + planes


def _tooltype_region(data: bytes) -> tuple[int, int, list[str]]:
    """Locate the tool type array, returning (start, end, entries)."""
    if len(data) < DISKOBJECT_SIZE or struct.unpack_from(">H", data, 0)[0] != MAGIC:
        raise InfoError("not an Amiga .info file")

    gadget = GADGET_OFFSET
    gadget_flags = struct.unpack_from(">H", data, gadget + 12)[0]
    gadget_render = struct.unpack_from(">I", data, gadget + 18)[0]
    select_render = struct.unpack_from(">I", data, gadget + 22)[0]
    default_tool = struct.unpack_from(">I", data, 50)[0]
    tool_types = struct.unpack_from(">I", data, 54)[0]
    drawer_data = struct.unpack_from(">I", data, 66)[0]

    offset = DISKOBJECT_SIZE
    if drawer_data:
        offset += DRAWERDATA_SIZE
    if gadget_render:
        offset += _image_size(data, offset)
    #  A second image follows whenever gg_SelectRender is set.  Tying this to
    #  the gadget's highlight mode instead looks reasonable and is wrong: real
    #  icons carry two images with GADGHCOMP set, and skipping the second one
    #  puts every following offset out by the size of a bitplane.
    if select_render:
        offset += _image_size(data, offset)
    del gadget_flags

    if default_tool:
        length = struct.unpack_from(">I", data, offset)[0]
        offset += 4 + length

    if not tool_types:
        return offset, offset, []

    start = offset
    count_field = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    count = max(0, count_field // 4 - 1)
    entries: list[str] = []
    for _ in range(count):
        length = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        raw = data[offset:offset + length]
        offset += length
        entries.append(raw.split(b"\0")[0].decode("latin-1"))
    return start, offset, entries


def read_tooltypes(data: bytes) -> list[str]:
    return _tooltype_region(data)[2]


def write_tooltypes(data: bytes, entries: list[str]) -> bytes:
    """Return a copy of ``data`` with its tool types replaced."""
    start, end, _old = _tooltype_region(data)
    block = bytearray()
    block += struct.pack(">I", (len(entries) + 1) * 4)
    for entry in entries:
        raw = entry.encode("latin-1", errors="replace") + b"\0"
        block += struct.pack(">I", len(raw)) + raw
    out = bytearray(data[:start]) + block + bytearray(data[end:])
    #  do_ToolTypes must be non-zero for Workbench to read the array at all.
    struct.pack_into(">I", out, 54, 1 if entries else 0)
    return bytes(out)


def set_tooltype(data: bytes, key: str, value: str) -> bytes:
    """Set ``KEY=value``, replacing any existing entry for that key."""
    entries = read_tooltypes(data)
    prefix = key.upper() + "="
    updated: list[str] = []
    replaced = False
    for entry in entries:
        stripped = entry.lstrip("(").upper()
        if stripped.startswith(prefix):
            if not replaced:
                updated.append(f"{key}={value}")
                replaced = True
            continue
        updated.append(entry)
    if not replaced:
        updated.insert(0, f"{key}={value}")
    return write_tooltypes(data, updated)
