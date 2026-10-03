"""PiMiga as the source of a PiStorm card.

PiMiga is a Raspberry Pi image running Amiberry, an Amiga emulator, over four
drives kept as host folders: System, Demos, Games and Work.  A card built from
it takes those folders - the system whole, adapted for real hardware - and
nothing of the Linux around them.

What PiMiga needs from the machine is read from PiMiga itself: the Amiberry
configuration that mounts its System drive says which processor, FPU and
graphics card it was set up for.  Only where that cannot be found are the
requirements assumed, and then as the ones it ships with.
"""
from __future__ import annotations

import dataclasses
import functools
import json
import re
import subprocess
from pathlib import Path

from . import presets
from .machines import Cpu

#  An Amiberry or WinUAE configuration: "key=value" lines.
CONFIG_LINE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")


@dataclasses.dataclass(frozen=True)
class Needs:
    """What the machine has to provide for PiMiga's system to run."""

    cpu: Cpu
    fpu: bool
    rtg: bool
    #  The Kickstart, as (lowest, highest) major version.  PiMiga's System is
    #  AmigaOS 3.9, which runs on 3.1 - and PiMiga ships no ROM of its own,
    #  only a placeholder asking for "your 1200 3.1 kickstart".
    kickstart: tuple[int, int] = (40, 40)
    #  Where these were read from; "" when they are the defaults.
    source: str = ""


#  What PiMiga 5 ships configured for, used only where its configuration
#  cannot be read: Pimiga5.uae asks for a 68040 with its FPU and a Zorro III
#  RTG card.
SHIPPED = Needs(Cpu.M68040, fpu=True, rtg=True)


def read_config(path: Path) -> dict[str, list[str]]:
    """An Amiberry configuration, every value of every key in order."""
    values: dict[str, list[str]] = {}
    try:
        text = path.read_text(encoding="latin-1")
    except OSError:
        return values
    for line in text.splitlines():
        found = CONFIG_LINE.match(line)
        if found and not line.lstrip().startswith(("#", ";")):
            values.setdefault(found.group(1).lower(), []).append(found.group(2))
    return values


def _mounts_system(values: dict[str, list[str]], system: Path) -> bool:
    """Whether a configuration mounts this System folder as a drive."""
    wanted = str(system).rstrip("/").lower()
    for key in ("filesystem2", "uaehf0", "uaehf1", "hardfile2"):
        for value in values.get(key, []):
            #  "rw,DH0:System:/home/pi/pimiga/disks/System,0": the host path
            #  is the third colon-separated field, before the boot priority.
            fields = value.split(":")
            if len(fields) >= 3:
                host = ":".join(fields[2:]).rsplit(",", 1)[0].strip().lower()
                #  The path the configuration names is the one inside the
                #  image; here it may be mounted anywhere, so its tail is
                #  what has to match.
                if host and (wanted.endswith(host) or host.endswith(
                        "/".join(wanted.split("/")[-4:]))):
                    return True
    return False


def needs(disks: Path) -> Needs:
    """What PiMiga's system needs, read from the configuration that runs it."""
    system = disks / "System"
    #  /home/pi/pimiga/disks -> /home/pi, where Amiberry keeps its "conf".
    home = disks.parent.parent
    candidates = sorted(home.glob("*/conf/*.uae")) + sorted(home.glob(
        ".config/*/conf/*.uae"))
    for path in candidates:
        values = read_config(path)
        if not _mounts_system(values, system):
            continue
        cpu_text = (values.get("cpu_model") or values.get("cpu_type")
                    or [""])[-1]
        try:
            cpu = Cpu(cpu_text.strip())
        except ValueError:
            cpu = SHIPPED.cpu
        fpu = (values.get("fpu_model") or ["none"])[-1].strip().lower() \
            not in ("", "none", "0")
        rtg = any(value.strip().lower() not in ("", "none", "0")
                  for value in values.get("gfxcard_type", [])
                  + values.get("gfxcard_size", []))
        return dataclasses.replace(SHIPPED, cpu=cpu, fpu=fpu, rtg=rtg,
                                   source=str(path))
    return SHIPPED


@functools.lru_cache(maxsize=8)
def needs_cached(disks: str) -> Needs:
    """``needs``, read once per PiMiga: every change to a setting validates."""
    return needs(Path(disks))


def problems(found: Needs, *, rtg_display: bool, on_a_pistorm: bool,
             cpu: Cpu, kickstart_version: int | None) -> list[str]:
    """What stops this machine running PiMiga's system, in plain words."""
    out: list[str] = []
    if found.rtg and not rtg_display:
        out.append("PiMiga's desktop is laid out for an RTG screen, so a "
                   "card built from it needs an RTG display - choose one on "
                   "the Machine step.")
    if found.rtg and not on_a_pistorm:
        out.append("PiMiga's RTG screen on real hardware is Emu68's, so a "
                   "card built from it needs a PiStorm.")
    if not cpu.at_least(found.cpu):
        out.append(f"PiMiga is set up for a {found.cpu.label}"
                   f"{' with an FPU' if found.fpu else ''}, and this machine "
                   f"has a {cpu.label}.")
    if kickstart_version is not None and not (
            found.kickstart[0] <= kickstart_version <= found.kickstart[1]):
        out.append(f"PiMiga's system is AmigaOS 3.9, which runs on Kickstart "
                   f"3.1, and the chosen Kickstart is V{kickstart_version}. "
                   f"Choose your machine's 3.1 ROM.")
    return out


# ----------------------------------------------------------------- the image

#  Where PiMiga keeps its drives inside the image's Linux partition.
DISKS_IN_IMAGE = Path("home/pi/pimiga/disks")


def _loops_for(image: Path) -> list[str]:
    """Loop devices already backed by this image, if any.

    losetup reports the backing file relative to the mount it lives on, so
    only its name can be compared.
    """
    try:
        listed = subprocess.run(["losetup", "--json", "--list"],
                                capture_output=True, text=True, check=False)
        devices = json.loads(listed.stdout or "{}").get("loopdevices", [])
    except (OSError, ValueError):
        return []
    return [device["name"] for device in devices
            if Path(str(device.get("back-file") or "")).name == image.name]


def _mountpoints(device: str) -> list[Path]:
    """Where each partition of a loop device is mounted."""
    try:
        listed = subprocess.run(["lsblk", "--json", "-o", "NAME,MOUNTPOINT",
                                 device], capture_output=True, text=True,
                                check=False)
        tree = json.loads(listed.stdout or "{}").get("blockdevices", [])
    except (OSError, ValueError):
        return []
    out: list[Path] = []
    for node in tree:
        for part in node.get("children", []) or []:
            if part.get("mountpoint"):
                out.append(Path(part["mountpoint"]))
    return out


def _partitions(device: str) -> list[str]:
    try:
        listed = subprocess.run(["lsblk", "--json", "-o", "NAME,FSTYPE",
                                 device], capture_output=True, text=True,
                                check=False)
        tree = json.loads(listed.stdout or "{}").get("blockdevices", [])
    except (OSError, ValueError):
        return []
    return [f"/dev/{part['name']}" for node in tree
            for part in node.get("children", []) or []
            if part.get("fstype") == "ext4"]


def disks_in(path: str | Path) -> Path | None:
    """PiMiga's drives, given a folder holding them or a PiMiga image.

    An image already mounted - as a desktop mounts one that is double
    clicked - is used where it is.  Otherwise it is attached read-only
    through udisks, which needs no root, and its Linux partition mounted
    read-only; it is left mounted, the way the desktop leaves it.
    """
    path = Path(path)
    if path.is_dir():
        return presets.pimiga_disks(path)
    if not path.is_file():
        return None
    for device in _loops_for(path):
        for mountpoint in _mountpoints(device):
            found = presets.pimiga_disks(mountpoint / DISKS_IN_IMAGE)
            if found is not None:
                return found
    attached = subprocess.run(
        ["udisksctl", "loop-setup", "--read-only", "--no-user-interaction",
         "-f", str(path)], capture_output=True, text=True, check=False)
    found_loop = re.search(r"(/dev/loop\d+)", attached.stdout)
    if attached.returncode != 0 or found_loop is None:
        raise RuntimeError(f"{path.name} could not be attached: "
                           f"{(attached.stderr or attached.stdout).strip()}")
    for partition in _partitions(found_loop.group(1)):
        mounted = subprocess.run(
            ["udisksctl", "mount", "--no-user-interaction", "-o", "ro",
             "-b", partition], capture_output=True, text=True, check=False)
        if mounted.returncode != 0:
            #  The likeliest reason: PiMiga was not shut down cleanly, and a
            #  journal that needs replaying cannot be read only.
            raise RuntimeError(
                f"The Linux partition in {path.name} would not mount "
                f"read-only ({mounted.stderr.strip()}). If PiMiga was not "
                f"shut down cleanly, mount the image once with your file "
                f"manager, then choose the folder it gives you.")
    for mountpoint in _mountpoints(found_loop.group(1)):
        found = presets.pimiga_disks(mountpoint / DISKS_IN_IMAGE)
        if found is not None:
            return found
    return None
