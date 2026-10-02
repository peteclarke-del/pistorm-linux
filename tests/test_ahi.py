"""AHI's settings, saved for it, with the mode its driver lists."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import ahi, packages  # noqa: E402


def modes_file(*modes: tuple[int, str, bool, bool]) -> bytes:
    """A DEVS:AudioModes file the way AHI's drivers ship them."""
    body = b"AHIM" + b"AUDN" + struct.pack(">I", 6) + b"paula\0"
    for audio_id, name, stereo, panning in modes:
        tags = [(ahi.AHIDB_AUDIO_ID, audio_id),
                (ahi.AHIDB_PANNING, int(panning)),
                (ahi.AHIDB_STEREO, int(stereo))]
        offset = (len(tags) + 2) * 8
        tags.append((ahi.AHIDB_NAME, offset))
        chunk = b"".join(struct.pack(">II", t, v) for t, v in tags)
        chunk += bytes(8) + name.encode("latin-1") + b"\0"
        body += b"AUDM" + struct.pack(">I", len(chunk)) + chunk
        body += b"\0" * (len(chunk) & 1)
    return b"FORM" + struct.pack(">I", len(body)) + body


def chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    out, pos = [], 12
    while pos < len(data):
        size = struct.unpack_from(">I", data, pos + 4)[0]
        out.append((data[pos:pos + 4], data[pos + 8:pos + 8 + size]))
        pos += 8 + size + (size & 1)
    return out


class TheModeComesFromTheDriver(unittest.TestCase):

    def test_the_first_stereo_mode_with_panning_is_chosen(self):
        listed = ahi.audio_modes(modes_file(
            (0x20005, "Paula:14 bit mono", False, False),
            (0x20003, "Paula:14 bit stereo", True, False),
            (0x20001, "Paula:14 bit stereo++", True, True),
            (0x20007, "Paula:8 bit stereo++", True, True)))
        self.assertEqual([m.name for m in listed][2], "Paula:14 bit stereo++")
        self.assertEqual(ahi.stereo_plus_plus(listed).id, 0x20001)

    def test_a_driver_with_no_stereo_plus_plus_has_no_choice(self):
        self.assertIsNone(ahi.stereo_plus_plus(ahi.audio_modes(modes_file(
            (0x20005, "Paula:14 bit mono", False, False)))))

    def test_something_else_is_refused(self):
        with self.assertRaises(ValueError):
            ahi.audio_modes(b"FORM\0\0\0\4ILBM")


class TheSettingsAreAHIPrefsOwn(unittest.TestCase):

    def test_the_layout_is_ahi_h(self):
        data = ahi.prefs(0x20001)
        self.assertEqual(data[:4] + data[8:12], b"FORMPREF")
        found = chunks(data)
        self.assertEqual([c for c, _b in found],
                         [b"PRHD", b"AHIG"] + [b"AHIU"] * len(ahi.UNITS))
        self.assertEqual(len(found[0][1]), 6, "struct PrefHeader")
        self.assertEqual(len(found[1][1]), 22, "struct AHIGlobalPrefs")
        for _c, unit in found[2:]:
            self.assertEqual(len(unit), 32, "struct AHIUnitPrefs")
            self.assertEqual(struct.unpack_from(">I", unit, 4)[0], 0x20001)
        self.assertEqual([u[0] for _c, u in found[2:]], list(ahi.UNITS),
                         "the four device units and the music unit")

    def test_the_ahi_package_saves_them(self):
        made = packages.CATALOGUE_BY_KEY["ahi"].download.made
        self.assertEqual([(m.destination, m.name) for m in made],
                         [("Prefs/Env-Archive/Sys", "ahi.prefs")])


if __name__ == "__main__":
    unittest.main()
