"""PiMiga as the source of a card: what it needs, and what is taken from it."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import amigaos, content, machines, packages, pimiga  # noqa: E402
from pistorm_imager.core.machines import Cpu, Display  # noqa: E402

#  PiMiga 5's own configuration, cut to the lines that matter here.
PIMIGA5_UAE = """\
kickstart_rom_file=/boot/firmware/kick/kick.rom
chipset=aga
cpu_type=68040
cpu_model=68040
fpu_model=68040
gfxcard_type=ZorroIII
gfxcard_size=128
filesystem2=rw,DH0:System:/home/pi/pimiga/disks/System,0
"""


class _Scratch(unittest.TestCase):
    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-pimiga-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder

    def pimiga(self, config: str | None = PIMIGA5_UAE) -> Path:
        """A PiMiga laid out as its image holds it, from /home/pi down."""
        home = self.scratch() / "home" / "pi"
        disks = home / "pimiga" / "disks"
        for drive in ("System", "Games", "Demos", "Work"):
            (disks / drive).mkdir(parents=True)
        if config is not None:
            (home / "Amiberry-Lite" / "conf").mkdir(parents=True)
            (home / "Amiberry-Lite" / "conf" / "Pimiga5.uae").write_text(config)
        return disks


class WhatPiMigaNeeds(_Scratch):
    """Read from the configuration that runs its System, not assumed."""

    def test_read_from_its_own_configuration(self):
        found = pimiga.needs(self.pimiga())
        self.assertEqual((found.cpu, found.fpu, found.rtg),
                         (Cpu.M68040, True, True))
        self.assertTrue(found.source.endswith("Pimiga5.uae"))

    def test_a_configuration_for_another_system_is_not_taken(self):
        found = pimiga.needs(self.pimiga(PIMIGA5_UAE.replace(
            "disks/System", "disks/Elsewhere").replace("68040", "68020")))
        self.assertEqual(found, pimiga.SHIPPED)

    def test_without_one_what_pimiga_ships_with_is_assumed(self):
        self.assertEqual(pimiga.needs(self.pimiga(None)), pimiga.SHIPPED)

    def test_a_pistorm_with_rtg_and_kickstart_31_is_enough(self):
        self.assertEqual(pimiga.problems(
            pimiga.SHIPPED, rtg_display=True, on_a_pistorm=True,
            cpu=machines.PISTORM_CPU, kickstart_version=40), [])

    def test_each_requirement_is_refused_on_its_own(self):
        for given, said in (({"rtg_display": False}, "RTG display"),
                            ({"on_a_pistorm": False}, "PiStorm"),
                            ({"cpu": Cpu.M68020}, "68040"),
                            ({"kickstart_version": 47}, "3.1")):
            ok = {"rtg_display": True, "on_a_pistorm": True,
                  "cpu": machines.PISTORM_CPU, "kickstart_version": 40}
            problems = pimiga.problems(pimiga.SHIPPED, **{**ok, **given})
            self.assertEqual(len(problems), 1, given)
            self.assertIn(said, problems[0], given)

    def test_a_folder_holding_it_is_found(self):
        disks = self.pimiga()
        for given in (disks, disks.parent, disks.parent.parent / "pimiga"):
            self.assertEqual(pimiga.disks_in(given), disks)
        self.assertIsNone(pimiga.disks_in(disks / "System"))


class OnlyWhatIsNeeded(_Scratch):
    """What an emulator setup brings that a real Amiga cannot use."""

    def test_uaes_own_records_are_never_copied(self):
        source = self.scratch() / "System"
        (source / "C").mkdir(parents=True)
        (source / "C" / "Dir").write_bytes(b"\0\0\x03\xf3 a command")
        (source / "_UAEFSDB.___").write_bytes(b"\x01" * 600)
        (source / "C" / "_UAEFSDB.___").write_bytes(b"\x01" * 600)
        reader = amigaos.FolderVolume(source)
        names = [relative for relative, _entry in reader.walk()]
        self.assertEqual(names, ["C", "C/Dir"])

    def test_host_run_drawers_are_offered_for_removal(self):
        """Amiberry starting Chrome on the Linux host means nothing here."""
        system = self.pimiga() / "System"
        fun = system / "Host Run fun"
        fun.mkdir()
        (fun / "chromium").write_text("c:host-run chromium\n")
        (fun / "VLC").write_text("host-run vlc player\n")
        (system / "Utilities").mkdir()
        (system / "Utilities" / "Clock").write_bytes(b"\0\0\x03\xf3 clock")
        reader = amigaos.FolderVolume(system)
        found = [c.path for c in content.clutter(reader)]
        self.assertIn("Host Run fun", found)
        self.assertNotIn("Utilities", found)

    def test_the_folder_reader_ignores_case_as_amigados_does(self):
        system = self.pimiga() / "System"
        (system / "Devs" / "Monitors").mkdir(parents=True)
        (system / "Devs" / "Monitors" / "uaegfx").write_bytes(b"monitor")
        reader = amigaos.FolderVolume(system)
        entry = reader.find("DEVS/monitors/UAEGFX")
        self.assertIsNotNone(entry)
        self.assertEqual(reader.read_file(entry), b"monitor")
        self.assertIsNone(reader.find("Devs/Monitors/VideoCore"))


class GettingOnline(unittest.TestCase):
    """Its stack was set up for the emulator's network; a PiStorm's is the Pi's."""

    def test_a_fetchable_stack_and_the_pis_own_interfaces(self):
        keys = packages.to_get_online(machines.MACHINES_BY_KEY["a1200"],
                                      Display.RTG_HDMI)
        chosen = [packages.CATALOGUE_BY_KEY[key] for key in keys]
        stacks = [p for p in chosen if p.role == packages.ROLE_TCP_IP_STACK]
        self.assertEqual(len(stacks), 1)
        self.assertFalse(stacks[0].download.manual,
                         "a manual download cannot be fetched by the build")
        self.assertTrue(any(
            item.destination.startswith(packages.NET_INTERFACES)
            for p in chosen if p not in stacks for item in p.download.write))


@unittest.skipUnless(shutil.which("udisksctl") and shutil.which("mkfs.ext4")
                     and shutil.which("sfdisk"),
                     "needs udisks and e2fsprogs to make and mount an image")
class FromItsImage(_Scratch):
    """An image is attached read-only and its Linux partition mounted."""

    def test_the_drives_are_found_inside_the_image(self):
        root = self.scratch()
        disks = self.pimiga()
        tree = disks.parent.parent.parent.parent       # above home/pi
        part = root / "part.ext4"
        image = root / "pimiga.img"
        subprocess.run(["truncate", "-s", "64M", str(part)], check=True)
        subprocess.run(["mkfs.ext4", "-q", "-d", str(tree), str(part)],
                       check=True)
        subprocess.run(["truncate", "-s", "70M", str(image)], check=True)
        subprocess.run(["sfdisk", "-q", str(image)], check=True, text=True,
                       input="label: dos\nstart=2048, size=131072, type=83\n")
        subprocess.run(["dd", f"if={part}", f"of={image}", "bs=512",
                        "seek=2048", "conv=notrunc", "status=none"],
                       check=True)
        try:
            found = pimiga.disks_in(image)
        except RuntimeError as error:
            self.skipTest(f"udisks would not attach it here: {error}")
        self.addCleanup(self.detach, image)
        self.assertIsNotNone(found)
        self.assertTrue((found / "System").is_dir())
        self.assertEqual(pimiga.needs(found).cpu, Cpu.M68040)
        self.assertEqual(pimiga.disks_in(image), found, "mounted once")

    @staticmethod
    def detach(image: Path) -> None:
        for device in pimiga._loops_for(image):
            for partition in pimiga._partitions(device):
                subprocess.run(["udisksctl", "unmount", "-b", partition,
                                "--no-user-interaction"], check=False,
                               capture_output=True)
            subprocess.run(["udisksctl", "loop-delete", "-b", device,
                            "--no-user-interaction"], check=False,
                           capture_output=True)


if __name__ == "__main__":
    unittest.main()
