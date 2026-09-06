"""Enumerating candidate SD cards, and the safety checks around writing to them.

Writing to the wrong block device destroys a disk, so the rules here are
deliberately conservative: only removable/hotplug or SD/USB-attached whole disks
are offered by default, and anything holding a mounted system directory is
refused outright regardless of what the user picked.
"""
from __future__ import annotations

import dataclasses
import errno
import json
import os
import shutil
import subprocess
from pathlib import Path

from .util import human_size

SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/home", "/usr", "/var", "/nix", "/boot/firmware"}


@dataclasses.dataclass
class Partition:
    path: str
    size: int
    fstype: str
    label: str
    mountpoints: list[str]


@dataclasses.dataclass
class Device:
    path: str
    name: str
    size: int
    model: str
    vendor: str
    transport: str
    removable: bool
    hotplug: bool
    read_only: bool
    partitions: list[Partition]

    @property
    def description(self) -> str:
        maker = " ".join(x for x in (self.vendor.strip(), self.model.strip()) if x)
        maker = maker or "Unknown device"
        via = f", {self.transport.upper()}" if self.transport else ""
        return f"{maker} - {human_size(self.size)} ({self.path}{via})"

    @property
    def mounted_paths(self) -> list[str]:
        out: list[str] = []
        for part in self.partitions:
            out += [m for m in part.mountpoints if m]
        return out

    @property
    def holds_system(self) -> bool:
        return any(m in SYSTEM_MOUNTS for m in self.mounted_paths)

    @property
    def likely_sd_card(self) -> bool:
        return self.removable or self.hotplug or self.transport in ("usb", "mmc", "sd")


def _lsblk() -> dict:
    if shutil.which("lsblk") is None:
        raise RuntimeError("lsblk is not available; cannot enumerate disks")
    columns = "NAME,PATH,SIZE,MODEL,VENDOR,TRAN,RM,HOTPLUG,TYPE,RO,FSTYPE,LABEL,MOUNTPOINTS"
    result = subprocess.run(["lsblk", "-J", "-b", "-o", columns],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"lsblk failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _mountpoints(node: dict) -> list[str]:
    points = node.get("mountpoints") or []
    return [p for p in points if p]


def list_devices(only_removable: bool = True) -> list[Device]:
    """List whole disks that could plausibly be the target SD card."""
    devices: list[Device] = []
    for node in _lsblk().get("blockdevices", []):
        if node.get("type") != "disk":
            continue
        partitions = [
            Partition(
                path=child.get("path") or "",
                size=int(child.get("size") or 0),
                fstype=child.get("fstype") or "",
                label=child.get("label") or "",
                mountpoints=_mountpoints(child),
            )
            for child in node.get("children", []) or []
            if child.get("type") == "part"
        ]
        device = Device(
            path=node.get("path") or f"/dev/{node.get('name')}",
            name=node.get("name") or "",
            size=int(node.get("size") or 0),
            model=node.get("model") or "",
            vendor=node.get("vendor") or "",
            transport=node.get("tran") or "",
            removable=bool(node.get("rm")),
            hotplug=bool(node.get("hotplug")),
            read_only=bool(node.get("ro")),
            partitions=partitions,
        )
        if device.size == 0:
            continue
        if only_removable and not device.likely_sd_card:
            continue
        devices.append(device)
    return sorted(devices, key=lambda d: d.path)


def check_writable(device: Device) -> None:
    """Raise if writing to this device would be obviously destructive or futile."""
    if device.read_only:
        raise RuntimeError(f"{device.path} is read-only (check the card's lock switch)")
    if device.holds_system:
        mounted = ", ".join(sorted(set(device.mounted_paths) & SYSTEM_MOUNTS))
        raise RuntimeError(
            f"Refusing to write to {device.path}: it currently provides {mounted}."
        )
    if not device.likely_sd_card:
        raise RuntimeError(
            f"{device.path} is not a removable device. Refusing to write to it."
        )


#  What the kernel returns once a card has stopped answering.  The card is
#  gone from the bus and every request against it fails the same way,
#  whichever direction it was going.
GONE_AWAY = {errno.EIO, errno.ENXIO, errno.ENODEV, errno.EREMOTEIO, errno.ETIMEDOUT}

CARD_STOPPED_ANSWERING = (
    "{path} stopped answering: {reason}.\n\n"
    "This is the card or the reader, not the card image. The kernel logs it "
    "as \"recovery failed\" and, when the card leaves the bus altogether, "
    "\"card removed\"; check with:\n"
    "    journalctl -k -b | grep -E 'mmcblk|recovery failed|card removed'\n\n"
    "Reseat the card, and try a USB card reader rather than a built-in slot - "
    "built-in readers run at the fastest UHS mode they can negotiate, and a "
    "marginal card or slot fails there and works over USB. A card that cannot "
    "be read reliably cannot be written reliably either, so it is worth "
    "proving this before spending an hour on a build."
)


def check_responds(device: Device, log=lambda text: None) -> None:
    """Read a few blocks, before an hour of work is spent on a card that cannot.

    ``check_writable`` asks whether writing *should* be allowed - the lock
    switch, a mounted system, a disk that is not removable. It never asked
    whether the card is actually there. A card that has dropped off the bus
    fails every request including sector 0, and the application saw only
    "Input/output error" an hour into a build, with nothing to say whether the
    fault was the card, the reader or this program.

    Reads only, and only a few of them: this must never be the thing that
    disturbs a card that was about to work.
    """
    size = device.size or 0
    spots = [0]
    if size > 2 * PROBE:
        spots += [(size // 2) & ~(PROBE - 1), size - PROBE]
    try:
        with open(device.path, "rb") as handle:
            for offset in spots:
                handle.seek(offset)
                if not handle.read(PROBE):
                    raise OSError(errno.EIO, "no data", device.path)
    except PermissionError:
        #  Not a verdict on the card. The build itself runs with privileges
        #  and will probe it properly there.
        log(f"Cannot probe {device.path} without privileges - skipped")
    except OSError as error:
        if error.errno in GONE_AWAY:
            raise RuntimeError(CARD_STOPPED_ANSWERING.format(
                path=device.path,
                reason=os.strerror(error.errno))) from error
        raise
    else:
        log(f"{device.path} answers at the start, middle and end")


PROBE = 4096                    # one page, aligned, at each of three spots


def unmount_all(device: Device, log=lambda text: None) -> None:
    """Unmount every mounted partition of the device before we overwrite it."""
    for part in device.partitions:
        for mountpoint in part.mountpoints:
            log(f"Unmounting {part.path} from {mountpoint}")
            result = subprocess.run(["udisksctl", "unmount", "-b", part.path],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                result = subprocess.run(["umount", part.path],
                                        capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Could not unmount {part.path}: "
                    f"{(result.stderr or result.stdout).strip()}"
                )


def reread_partition_table(path: str, log=lambda text: None) -> None:
    """Ask the kernel to pick up a partition table we just rewrote."""
    for argv in (["partx", "-u", path], ["blockdev", "--rereadpt", path]):
        if shutil.which(argv[0]) is None:
            continue
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode == 0:
            log(f"Re-read partition table via {argv[0]}")
            return
    log("Could not ask the kernel to re-read the partition table (harmless here)")


def device_size(path: str) -> int:
    """Size in bytes of a block device or regular file."""
    stat = os.stat(path)
    if not os.path.isfile(path):
        with open(path, "rb") as handle:
            return handle.seek(0, os.SEEK_END)
    return stat.st_size


def is_block_device(path: str | Path) -> bool:
    try:
        import stat as stat_module
        return stat_module.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False
