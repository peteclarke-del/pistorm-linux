"""Enumerating candidate SD cards, and the safety checks around writing to them.

Writing to the wrong block device destroys a disk, so the rules here are
deliberately conservative: only removable/hotplug or SD/USB-attached whole disks
are offered by default, and anything holding a mounted system directory is
refused outright regardless of what the user picked.
"""
from __future__ import annotations

import dataclasses
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
