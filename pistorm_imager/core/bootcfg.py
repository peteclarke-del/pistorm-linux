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

    def set_overlay(self, name: str, params: tuple[str, ...] = ()) -> None:
        """Load a device tree overlay, with its parameters on the same line.

        Emu68 1.1 takes its settings this way rather than from cmdline.txt.
        An existing line for the same overlay is rewritten in place and a
        commented-out example revived, exactly as :meth:`set` does for an
        ordinary key: the config.txt that ships with Emu68 carries the unicam
        overlay as a commented example, and reviving it keeps the author's
        comment above it with the setting it explains.
        """
        wanted = ",".join([name, *params])
        pattern = rf"^(\s*)(#\s*)?dtoverlay\s*=\s*{re.escape(name)}(,.*)?$"
        live, commented = [], []
        for index, line in enumerate(self.lines):
            if re.match(pattern, line):
                (commented if re.match(r"^\s*#", line) else live).append(index)
        if live:
            self.lines[live[0]] = f"dtoverlay={wanted}"
            for index in live[1:]:
                self.lines[index] = "#" + self.lines[index].lstrip("#")
        elif commented:
            self.lines[commented[0]] = f"dtoverlay={wanted}"
        else:
            self.lines.append(f"dtoverlay={wanted}")

    def anchor_overlay_params(self) -> None:
        """Make sure a ``dtparam=`` line belongs to the overlay it names.

        ``dtparam=`` applies to *the last overlay loaded*, so a ``dtparam=ant2``
        written for the Pi's own base overlay would silently become a parameter
        of whichever overlay this tool loaded before it - the CM4's external
        aerial turning into a parameter the unicam overlay has never heard of.
        Emu68's own config.txt shows the remedy: a bare ``dtoverlay=`` line
        re-references the base overlay, and that is what goes in front.
        """
        for index, line in enumerate(list(self.lines)):
            if not re.match(r"^\s*dtparam\s*=\s*ant[12]\s*$", line):
                continue
            previous = index - 1
            while previous >= 0 and not self.lines[previous].strip():
                previous -= 1
            if previous >= 0 and re.match(r"^\s*dtoverlay\s*=\s*$",
                                          self.lines[previous]):
                return
            self.lines.insert(index, "dtoverlay=")
            return

    def text(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"

    def to_bytes(self) -> bytes:
        return self.text().encode("utf-8")


#  The Raspberry Pi models each SD/eMMC overlay is for.  Emu68 ships two
#  drivers for the same job and one overlay each: ``brcm-sdhc.device`` on the
#  Pi 3 and Zero 2, ``brcm-emmc.device`` on the Pi 4 and CM4.  Naming them here
#  rather than in the builder keeps the fact with the file that writes it.
SD_OVERLAYS = {"pi3": "sdhc", "pi4": "emmc", "cm4": "emmc"}


def sd_overlay_for(pi_model: str) -> str:
    """Which SD card overlay drives this Pi, or "" when nobody has said."""
    return SD_OVERLAYS.get(pi_model.strip().lower(), "")


#  Settings that Emu68 1.1 took out of cmdline.txt and gave to a device tree
#  overlay.  These are not merely deprecated: the words are gone from the 1.1
#  kernel, so a card written with them is a card where the setting does
#  nothing and nothing says so.  Each entry names the overlay that carries the
#  setting now and the cmdline words it replaces.
#
#  Both halves of the decision are read from this one table - the overlay line
#  that gets written, and the words that get left out of cmdline.txt - because
#  a setting written in both forms, or in neither, is exactly the failure the
#  table exists to prevent.
MOVED_TO_OVERLAYS = {
    #  field on BootOptions -> (overlay, cmdline words it replaces)
    "unicam": ("unicam", ("unicam.boot", "unicam.smooth")),
    "z2_ram_size": ("z2ram", ("z2_ram_size",)),
    "sd_unit0_rw": ("", ("sd.unit0",)),          # the Pi decides which overlay
    "vbr_move": ("emu68", ("vbr_move",)),
}


def unicam_overlay_params(extra: str) -> list[str]:
    """Framethrower extras written as overlay parameters.

    The same settings are spelled ``unicam.w=640`` on a cmdline and ``w=640``
    on an overlay line.  A setting typed in either spelling is carried across
    rather than refused, because the person typing it is reading whichever of
    Emu68's two eras of documentation they happened to find.
    """
    out = []
    for word in extra.split():
        out.append(word[len("unicam."):] if word.startswith("unicam.") else word)
    return out


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
    #  Turn the Pi's onboard OTG socket into a host port.  Only meaningful
    #  where a USB stack is installed and pointed at unit 0 of xhci.device;
    #  without it that unit enumerates nothing at all.
    otg_mode: bool | None = None
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

    def overlay_handles(self, field: str, available: frozenset[str] = frozenset(),
                        sd_overlay: str = "") -> bool:
        """Whether this release takes ``field`` as an overlay parameter.

        Answering yes means two things at once and they must not come apart:
        the overlay line is written, and the cmdline words for the same setting
        are left out.
        """
        overlay, _words = MOVED_TO_OVERLAYS[field]
        if overlay:
            return overlay in available
        #  The SD card driver, whose overlay depends on which Pi is fitted.
        wanted = [sd_overlay] if sd_overlay else sorted(set(SD_OVERLAYS.values()))
        return any(name in available for name in wanted)

    def overlay_lines(self, available: frozenset[str] = frozenset(),
                      sd_overlay: str = "") -> list[tuple[str, tuple[str, ...]]]:
        """The ``dtoverlay=`` lines this setup needs, as (name, parameters).

        Only the settings that have somewhere to go are here.  Everything else
        stays in cmdline.txt, which the 1.1 kernel still reads: ``vc4.mem``,
        ``limit_2g``, ``swap_df0_with_df1``, ``chip_slowdown``, ``dbf_slowdown``,
        ``blitwait``, ``enable_c0_slow`` and ``move_slow_to_chip`` are all still
        in it, and moving working settings for the sake of tidiness would be a
        change with a risk and no gain.
        """
        lines: list[tuple[str, tuple[str, ...]]] = []
        if self.unicam and self.overlay_handles("unicam", available, sd_overlay):
            params = ["boot"]
            if self.unicam_smooth:
                params.append("smooth")
            params += unicam_overlay_params(self.unicam_extra)
            lines.append(("unicam", tuple(params)))
        if (self.z2_ram_size is not None
                and self.overlay_handles("z2_ram_size", available, sd_overlay)):
            lines.append(("z2ram", (f"size={self.z2_ram_size}",)))
        if self.sd_unit0_rw and self.overlay_handles("sd_unit0_rw", available,
                                                     sd_overlay):
            #  With no Pi named, every SD overlay the release ships is written.
            #  One of them is for the driver this board actually runs and the
            #  other is for a driver that is not there to read it; the setting
            #  reaching the card matters more than the tidier line.
            wanted = ([sd_overlay] if sd_overlay
                      else sorted(set(SD_OVERLAYS.values())))
            for name in wanted:
                if name in available:
                    lines.append((name, ("unit0=rw",)))
        if self.vbr_move and self.overlay_handles("vbr_move", available, sd_overlay):
            lines.append(("emu68", ("vbr_move",)))
        return lines

    def apply_config(self, config: ConfigTxt, *,
                     overlays: frozenset[str] = frozenset(),
                     sd_overlay: str = "") -> ConfigTxt:
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
        if self.otg_mode is not None:
            config.set("otg_mode", "1" if self.otg_mode else "0",
                       comment="Host mode on the onboard USB OTG port")
        if self.kickstart_file:
            config.set("initramfs", self.kickstart_file, separator=" ",
                       comment="Kickstart ROM mapped by Emu68 (maprom)")
        else:
            config.comment_out("initramfs")
        lines = self.overlay_lines(overlays, sd_overlay)
        if lines:
            #  Before the first overlay of ours goes in, make sure the aerial
            #  parameter cannot be captured by it.
            config.anchor_overlay_params()
        for name, params in lines:
            config.set_overlay(name, params)
        return config

    def cmdline(self, *, overlays: frozenset[str] = frozenset(),
                sd_overlay: str = "") -> str:
        """The cmdline.txt line, leaving out whatever the overlays now carry."""
        def moved(field: str) -> bool:
            return self.overlay_handles(field, overlays, sd_overlay)

        parts: list[str] = []
        if self.vc4_mem is not None:
            parts.append(f"vc4.mem={self.vc4_mem}")
        if self.vbr_move and not moved("vbr_move"):
            parts.append("vbr_move")
        if self.limit_2g:
            parts.append("limit_2g")
        if self.z2_ram_size is not None and not moved("z2_ram_size"):
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
        if self.sd_unit0_rw and not moved("sd_unit0_rw"):
            parts.append("sd.unit0=rw")
        if self.unicam and not moved("unicam"):
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
