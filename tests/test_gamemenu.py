"""The ArcadeGameSelector 2 menu, written from the games on a drive.

AGS2 shows a drawer tree and nothing else, so the build writes one: an entry
per WHDLoad game, in the drawers the collection is already sorted into, named
from the slave and started with the game's own icon settings.
"""
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pistorm_imager.core import gamemenu  # noqa: E402
from pistorm_imager.core.util import Progress  # noqa: E402
from test_compat import make_icon  # noqa: E402

WORDS = {"slave", "preload", "custom", "quitkey", "data", "ntsc"}


def slave(name: str, copyright_: str = "", info: str = "",
          version: int = 17) -> bytes:
    """A slave header the way WHDLoad's include file lays it out."""
    head = bytearray(b"\x70\xff\x4e\x75" + b"WHDLOADS")
    head += struct.pack(">HH", version, 0) + bytes(48 - len(head) - 4)
    strings = bytearray()
    offsets = []
    for text in (name, copyright_, info):
        offsets.append(len(head) + 4 + len(strings) if text else 0)
        strings += text.encode("latin-1").replace(b"\n", b"\xff") + b"\0"
    for field, offset in zip(gamemenu.NAME_FIELDS, offsets):
        struct.pack_into(">H", head, field, offset)
    return bytes(head) + bytes(4) + bytes(strings)


class TheMenu(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pistorm-menu-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.drive = self.root / "Games"

    def game(self, where: str, slave_name: str, data: bytes,
             tooltypes: list[str] | None = None, picture: bytes = b""):
        drawer = self.drive / where
        drawer.mkdir(parents=True)
        (drawer / slave_name).write_bytes(data)
        if tooltypes is not None:
            (drawer / (drawer.name + ".info")).write_bytes(
                make_icon(tooltypes))
        if picture:
            (drawer / "igame.iff").write_bytes(picture)
        return drawer

    def build(self, layout=gamemenu.AGA, media=False, leave_out=()):
        into = self.root / "menu"
        count = gamemenu.build(
            [gamemenu.Drive("Games", self.drive, tuple(leave_out),
                            "WHDLoad")], into,
            layout, WORDS, media, Progress())
        return into, count

    def test_the_menu_follows_the_collection_and_names_from_the_slave(self):
        self.game("WHDLoad/OCS/A/Agony", "Agony.slave",
                  slave("Agony", "1992 Psygnosis"), ["SLAVE=Agony.slave"])
        into, count = self.build()
        self.assertEqual(count, 1)
        entry = into / "OCS.ags" / "A.ags" / "Agony.run"
        self.assertTrue(entry.is_file(),
                        "the WHDLoad drawer is dropped and the rest kept")
        script = entry.read_text(encoding="latin-1")
        self.assertIn('Cd "Games:WHDLoad/OCS/A/Agony"', script)
        self.assertIn('WHDLoad SLAVE="Agony.slave"', script)

    def test_the_icon_settings_become_the_command_line(self):
        #  WHDLoad reads tool types only from Workbench; AGS2 starts it from
        #  a script, so they have to be written into it - and only the ones
        #  that are WHDLoad's, or it refuses the whole line.
        self.game("WHDLoad/A/Agony", "Agony.slave", slave("Agony"),
                  ["SLAVE=Agony.slave", "PRELOAD", "Custom1=2",
                   "QuitKey=$59", "*** DON'T EDIT THE FOLLOWING LINES!! ***",
                   "IM1=imagedata", "SomethingElse=1"])
        into, _ = self.build()
        script = (into / "A.ags" / "Agony.run").read_text(encoding="latin-1")
        self.assertIn("PRELOAD Custom1=2 QuitKey=$59", script)
        self.assertNotIn("IM1", script)
        self.assertNotIn("SomethingElse", script)

    def test_language_versions_are_told_apart_by_their_drawers(self):
        for drawer in ("KickOff2De", "KickOff2Fr"):
            self.game(f"WHDLoad/K/{drawer}", f"{drawer}.slave",
                      slave("Kick Off 2"), [f"SLAVE={drawer}.slave"])
        self.game("WHDLoad/K/KickOff3", "KickOff3.slave",
                  slave("Kick Off 3"), ["SLAVE=KickOff3.slave"])
        into, _ = self.build()
        names = sorted(p.name for p in (into / "K.ags").glob("*.run"))
        self.assertEqual(names, ["Kick Off 2 (KickOff2De).run",
                                 "Kick Off 2 (KickOff2Fr).run",
                                 "Kick Off 3.run"])

    def test_what_the_drive_leaves_out_the_menu_leaves_out(self):
        self.game("WHDLoad/AGA/A/Alien", "Alien.slave", slave("Alien"), [])
        self.game("WHDLoad/OCS/A/Agony", "Agony.slave", slave("Agony"), [])
        into, count = self.build(leave_out=("WHDLoad/AGA",))
        self.assertEqual(count, 1)
        self.assertFalse((into / "AGA.ags").exists())

    def test_pictures_and_text_only_when_asked(self):
        picture = gamemenu.plain_picture(320, 256, 8)
        self.game("WHDLoad/A/Agony", "Agony.slave",
                  slave("Agony", "1992 Psygnosis", "Installed by Someone"),
                  ["SLAVE=Agony.slave"], picture=picture)
        into, _ = self.build(media=False)
        self.assertFalse((into / "A.ags" / "Agony.iff").exists())
        self.assertFalse((into / "A.ags" / "Agony.txt").exists())
        shutil.rmtree(into)
        into, _ = self.build(media=True)
        self.assertEqual((into / "A.ags" / "Agony.iff").read_bytes(), picture)
        text = (into / "A.ags" / "Agony.txt").read_text(encoding="latin-1")
        self.assertIn("1992 Psygnosis", text)

    def test_a_picture_the_layout_cannot_show_is_left_out(self):
        #  The 16-colour layout has a 320x128 box; a WHDLoad screenshot is a
        #  full screen in 256 colours.
        self.game("WHDLoad/A/Agony", "Agony.slave", slave("Agony"),
                  ["SLAVE=Agony.slave"],
                  picture=gamemenu.plain_picture(320, 256, 8))
        into, _ = self.build(layout=gamemenu.NATIVE, media=True)
        self.assertFalse((into / "A.ags" / "Agony.iff").exists())

    def test_the_layout_brings_its_settings_and_pictures(self):
        self.game("WHDLoad/A/Agony", "Agony.slave", slave("Agony"), [])
        into, _ = self.build(layout=gamemenu.AGA)
        self.assertIn("screenshot_x = 320",
                      (into / "AGS2.conf").read_text())
        self.assertEqual(gamemenu.picture_size(
            (into / "Menu-Background.iff").read_bytes()), (640, 256, 8))

    def test_an_old_slave_falls_back_to_its_drawer(self):
        self.game("WHDLoad/A/OldGame", "OldGame.slave",
                  slave("Ignored", version=8), [])
        into, _ = self.build()
        self.assertTrue((into / "A.ags" / "OldGame.run").exists())


if __name__ == "__main__":
    unittest.main()
