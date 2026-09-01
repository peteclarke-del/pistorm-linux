"""Editing the Raspberry Pi ``config.txt`` and the Emu68 ``cmdline.txt``.

We deliberately *edit* the config.txt that ships inside the Emu68 release rather
than generating one from scratch: upstream keeps useful comments there and adds
new keys between versions, and a hand-written replacement would silently drop
them.  Setting a key rewrites it in place if present (uncommenting it if it was
commented out) and appends it otherwise.
"""
from __future__ import annotations

import dataclasses
import re

#  Raspberry Pi HDMI modes.  group 1 = CEA (TV timings), group 2 = DMT (monitor).
HDMI_MODES: list[tuple[str, int | None, int | None]] = [
    ("Automatic (use the monitor's EDID)", None, None),
    ("640 x 480 @ 60Hz", 2, 4),
    ("800 x 600 @ 60Hz", 2, 9),
    ("1024 x 768 @ 60Hz", 2, 16),
    ("1280 x 720 @ 60Hz (720p)", 2, 85),
    ("1280 x 800 @ 60Hz", 2, 28),
    ("1280 x 1024 @ 60Hz", 2, 35),
    ("1360 x 768 @ 60Hz", 2, 39),
    ("1366 x 768 @ 60Hz", 2, 81),
    ("1440 x 900 @ 60Hz", 2, 47),
    ("1600 x 1200 @ 60Hz", 2, 51),
    ("1680 x 1050 @ 60Hz", 2, 58),
    ("1920 x 1080 @ 60Hz (1080p)", 2, 82),
    ("1920 x 1200 @ 60Hz", 2, 69),
    ("720p 50Hz (TV)", 1, 19),
    ("1080p 50Hz (TV)", 1, 31),
    ("1080p 60Hz (TV)", 1, 16),
]


class ConfigTxt:
    """A line-preserving editor for Raspberry Pi config.txt files."""

    def __init__(self, text: str = ""):
        self.lines: list[str] = text.splitlines()

    @classmethod
    def load(cls, path) -> "ConfigTxt":
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return cls(handle.read())

    def _match(self, key: str, index: int) -> re.Match | None:
        #  Accept "key=value", "key value" (initramfs) and commented forms.
        pattern = rf"^(\s*)(#\s*)?({re.escape(key)})(\s*=\s*|\s+)(.*)$"
        return re.match(pattern, self.lines[index])

    def get(self, key: str) -> str | None:
        for index in range(len(self.lines)):
            match = self._match(key, index)
            if match and not match.group(2):
                return match.group(5).strip()
        return None

    def _occurrences(self, key: str) -> tuple[list[int], list[int]]:
        """Return (live line indices, commented-out line indices) for ``key``."""
        live, commented = [], []
        for index in range(len(self.lines)):
            match = self._match(key, index)
            if match:
                (commented if match.group(2) else live).append(index)
        return live, commented

    def set(self, key: str, value: str, *, separator: str = "=",
            comment: str | None = None) -> None:
        """Set ``key`` to ``value``.

        An existing active line is rewritten in place; failing that a
        commented-out example is revived.  Any further active duplicates are
        commented out, because config.txt files shipped with Emu68 carry several
        alternative examples of the same key and leaving two live copies (of
        ``initramfs``, say) is how you end up loading the wrong ROM.
        """
        replacement = f"{key}{separator}{value}"
        live, commented = self._occurrences(key)
        if live:
            target, rest = live[0], live[1:]
        elif commented:
            target, rest = commented[0], []
        else:
            if comment:
                self.lines.append("")
                self.lines.append(f"# {comment}")
            self.lines.append(replacement)
            return
        self.lines[target] = replacement
        for index in rest:
            self.lines[index] = "#" + self.lines[index]

    def comment_out(self, key: str) -> None:
        for index in range(len(self.lines)):
            match = self._match(key, index)
            if match and not match.group(2):
                self.lines[index] = "#" + self.lines[index]

    def remove(self, key: str) -> None:
        self.lines = [line for index, line in enumerate(self.lines)
                      if not (self._match(key, index) and not self._match(key, index).group(2))]

    def set_antenna(self, external: bool) -> None:
        """Select the CM4 antenna without disturbing unrelated dtparam lines."""
        wanted = "dtparam=ant2" if external else "dtparam=ant1"
        found = False
        for index, line in enumerate(self.lines):
            if re.match(r"^\s*#?\s*dtparam\s*=\s*ant[12]\s*$", line):
                self.lines[index] = wanted if not found else "#" + line.lstrip("#")
                found = True
        if not found:
            self.lines.append(wanted)

    def text(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"

    def to_bytes(self) -> bytes:
        return self.text().encode("utf-8")


@dataclasses.dataclass
class BootOptions:
    """Everything the imager can put into config.txt / cmdline.txt.

    Every config.txt field defaults to ``None``, meaning *leave whatever the
    Emu68 release shipped*.  Upstream tunes these per release - 1.0.7 caps
    memory at 2 GB and leaves the overclock commented out, 1.1 drops the cap and
    turns the overclock on - so silently imposing our own values would quietly
    undo the author's choices.  Only the kernel name and the Kickstart line are
    always managed, because those are ours to control.
    """

    kernel: str | None = None
    kickstart_file: str | None = None          # e.g. "kick.rom"; None removes maprom
    hdmi_group: int | None = None
    hdmi_mode: int | None = None
    hdmi_automatic: bool = False               # True comments the mode out (use EDID)
    hdmi_force_hotplug: bool | None = None
    boot_delay: int | None = None
    gpu_mem: int | None = None
    total_mem: int | None = None               # MB
    overclock: bool | None = None
    cm4_external_antenna: bool | None = None
    #  cmdline.txt options (see Emu68 docs/Options.md)
    vc4_mem: int | None = None                 # MB reported to Picasso96
    vbr_move: bool = False
    limit_2g: bool = False
    z2_ram_size: int | None = None
    swap_df0_with_df1: bool = False
    chip_slowdown: bool = False
    #  Emu68's other two timing brakes.  A PiStorm runs the 68k far faster than
    #  any real Amiga, and OCS and ECS era software that times itself against
    #  the hardware - a DBF delay loop, a blitter it never waits for - breaks
    #  on speed alone.  ECS is not exempt: an A500+ or an A600 runs the same
    #  software the same way.  Emu68 accepts "dbf_slowdown" (DBF) and "blitwait" (BW)
    #  for exactly that, alongside "chip_slowdown" (SC).
    dbf_slowdown: bool = False
    blitwait: bool = False
    enable_slow_ram: bool = False
    sd_unit0_rw: bool = False
    unicam: bool = False
    unicam_smooth: bool = False
    unicam_extra: str = ""
    extra_cmdline: str = ""

    def apply_config(self, config: ConfigTxt) -> ConfigTxt:
        if self.kernel:
            config.set("kernel", self.kernel)
        if self.boot_delay is not None:
            config.set("boot_delay", str(self.boot_delay))
        if self.gpu_mem is not None:
            config.set("gpu_mem", str(self.gpu_mem))
        if self.total_mem is not None:
            config.set("total_mem", str(self.total_mem))
        if self.hdmi_force_hotplug is not None:
            config.set("hdmi_force_hotplug", "1" if self.hdmi_force_hotplug else "0")
        if self.hdmi_automatic:
            #  Let the firmware read the monitor's EDID instead of forcing timings.
            config.comment_out("hdmi_group")
            config.comment_out("hdmi_mode")
        elif self.hdmi_group and self.hdmi_mode:
            config.set("hdmi_group", str(self.hdmi_group))
            config.set("hdmi_mode", str(self.hdmi_mode))
        if self.overclock is not None:
            for key in ("force_turbo", "over_voltage", "arm_freq"):
                config.comment_out(key)
            if self.overclock:
                config.set("force_turbo", "1")
                config.set("over_voltage", "4")
                config.set("arm_freq", "1800")
        if self.cm4_external_antenna is not None:
            config.set_antenna(self.cm4_external_antenna)
        if self.kickstart_file:
            config.set("initramfs", self.kickstart_file, separator=" ",
                       comment="Kickstart ROM mapped by Emu68 (maprom)")
        else:
            config.comment_out("initramfs")
        return config

    def cmdline(self) -> str:
        parts: list[str] = []
        if self.vc4_mem is not None:
            parts.append(f"vc4.mem={self.vc4_mem}")
        if self.vbr_move:
            parts.append("vbr_move")
        if self.limit_2g:
            parts.append("limit_2g")
        if self.z2_ram_size is not None:
            parts.append(f"z2_ram_size={self.z2_ram_size}")
        if self.swap_df0_with_df1:
            parts.append("swap_df0_with_df1")
        if self.chip_slowdown:
            parts.append("chip_slowdown")
        if self.dbf_slowdown:
            parts.append("dbf_slowdown")
        if self.blitwait:
            parts.append("blitwait")
        if self.enable_slow_ram:
            parts += ["enable_c0_slow", "enable_c8_slow", "enable_d0_slow"]
        if self.sd_unit0_rw:
            parts.append("sd.unit0=rw")
        if self.unicam:
            parts.append("unicam.boot")
            if self.unicam_smooth:
                parts.append("unicam.smooth")
            if self.unicam_extra.strip():
                parts.append(self.unicam_extra.strip())
        if self.extra_cmdline.strip():
            parts.append(self.extra_cmdline.strip())
        return " ".join(parts)


def wifi_config(ssid: str, password: str, country: str = "GB") -> str:
    """A ``wpa_supplicant.conf`` for the Amiga-side PiStorm WiFi tooling."""
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    return (
        f"country={country}\n"
        "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
        "update_config=1\n"
        "\n"
        "network={\n"
        f'    ssid="{escape(ssid)}"\n'
        f'    psk="{escape(password)}"\n'
        "    key_mgmt=WPA-PSK\n"
        "}\n"
    )
