"""Unit tests for the parts that write bytes to a card.

Run with:  python3 -m unittest discover -s tests -v
"""
import io
import os
import struct
import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import bootcfg, builder, fat32, jobs, kickstart, mbr, rdb  # noqa: E402
from pistorm_imager.core.util import GIB, MIB, Progress, parse_size  # noqa: E402

QUIET = Progress()


class _Scratch(unittest.TestCase):
    """Base class giving each test a temporary directory that is cleaned up.

    The full-build tests each write a multi-gigabyte sparse card image; left
    behind, a few runs quietly fill /tmp.
    """

    def scratch(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pistorm-test-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder



def make_fat32(folder: Path, size: int) -> Path:
    path = folder / "fat.img"
    with open(path, "wb") as handle:
        handle.truncate(size)
    subprocess.run(["mkfs.vfat", "-F", "32", "-n", "TEST", str(path)],
                   check=True, capture_output=True)
    return path


class TestSizes(_Scratch):
    def test_parse_size(self):
        self.assertEqual(parse_size("256M"), 256 * MIB)
        self.assertEqual(parse_size("1.5G"), int(1.5 * GIB))
        self.assertEqual(parse_size("2048"), 2048 * MIB)  # bare numbers are MiB
        self.assertEqual(parse_size("512K"), 512 * 1024)

    def test_decimal_and_binary_units_differ(self):
        """A card sold as 32 GB is not 32 GiB, and the gap is over 2 GB."""
        self.assertEqual(parse_size("32GB"), 32 * 1000 ** 3)
        self.assertEqual(parse_size("32GiB"), 32 * GIB)
        self.assertEqual(parse_size("32G"), 32 * GIB)
        self.assertGreater(parse_size("32GiB") - parse_size("32GB"), 2 * 10 ** 9)

    def test_an_image_sized_in_gb_fits_the_card_it_names(self):
        from pistorm_imager.core.util import fits_card
        self.assertTrue(fits_card(parse_size("32GB"), 32))
        self.assertFalse(fits_card(parse_size("32GiB"), 32),
                         "a 32 GiB image is too big for a 32 GB card")

    def test_gb_is_not_silently_misread(self):
        """It used to raise, and the caller fell back to a default size."""
        for text in ("64GB", "1.5GB", "512MB", "2TB"):
            self.assertGreater(parse_size(text), 0, text)

    def test_nonsense_is_refused_rather_than_guessed(self):
        for text in ("", "banana", "12X", "-5G"):
            with self.assertRaises(ValueError, msg=text):
                parse_size(text)


class TestFat32(_Scratch):
    def setUp(self):
        self.path = make_fat32(self.scratch(), 96 * MIB)
        self.handle = open(self.path, "r+b")
        self.fs = fat32.Fat32(self.handle)

    def tearDown(self):
        self.handle.close()

    def test_write_and_read_back(self):
        payload = os.urandom(1_500_000)
        self.fs.write_bytes("kick.rom", payload)
        self.assertEqual(self.fs.read_bytes("kick.rom"), payload)

    def test_long_file_names_survive(self):
        name = "bcm2711-rpi-4-b.dtb"
        self.fs.write_bytes(name, b"x" * 100)
        self.assertIn(name, [e.name for e in self.fs.listdir("/")])

    def test_lower_case_short_names_keep_their_case(self):
        self.fs.write_bytes("config.txt", b"a=1\n")
        self.fs.makedirs("kick")
        names = [e.name for e in self.fs.listdir("/")]
        self.assertIn("config.txt", names)
        self.assertIn("kick", names)

    def test_overwrite_frees_the_old_chain(self):
        self.fs.write_bytes("a.bin", b"x" * (200 * 1024))
        free_before = self.fs.free_bytes
        self.fs.write_bytes("a.bin", b"y" * (200 * 1024))
        self.assertEqual(self.fs.read_bytes("a.bin"), b"y" * (200 * 1024))
        self.assertEqual(self.fs.free_bytes, free_before)

    def test_subdirectories(self):
        self.fs.makedirs("a/b/c")
        self.fs.write_bytes("a/b/c/deep.txt", b"hello")
        self.assertEqual(self.fs.read_bytes("a/b/c/deep.txt"), b"hello")

    def test_fsck_is_happy(self):
        for index in range(30):
            self.fs.write_bytes(f"file-with-a-long-name-{index}.dat",
                                os.urandom(20_000))
        self.fs.flush()
        self.handle.flush()
        os.fsync(self.handle.fileno())
        result = subprocess.run(["fsck.vfat", "-n", str(self.path)],
                                capture_output=True, text=True)
        combined = result.stdout + result.stderr
        #  A stale free-cluster count is expected: we deliberately invalidate it.
        noise = [line for line in combined.splitlines()
                 if line.strip() and "Free cluster summary" not in line
                 and not line.startswith("fsck.fat")
                 and " files, " not in line]
        self.assertEqual(noise, [], combined)


class TestMbr(_Scratch):
    def test_round_trip(self):
        buffer = io.BytesIO(bytearray(1 * MIB))
        parts = [
            mbr.MbrPartition(0, 0x80, mbr.TYPE_FAT32_LBA, 8192, 524288),
            mbr.MbrPartition(1, 0x00, mbr.TYPE_AMIGA, 532480, 1000000),
        ]
        mbr.write_table(buffer, parts)
        back = mbr.read_table(buffer)
        self.assertEqual(back[0].type_id, mbr.TYPE_FAT32_LBA)
        self.assertEqual(back[0].start_lba, 8192)
        self.assertEqual(back[1].type_id, mbr.TYPE_AMIGA)
        self.assertEqual(back[1].sector_count, 1000000)
        self.assertTrue(back[2].empty)


class TestRdb(_Scratch):
    def test_layout_and_round_trip(self):
        geometry = rdb.Geometry()
        total = (16 * GIB) // 512
        parts = rdb.layout(geometry, total, [
            ("DH0", 2 * GIB, rdb.DOSTYPE_FFS_INTL),
            ("DH1", None, rdb.DOSTYPE_PFS3),
        ])
        table = rdb.Rdb(geometry=geometry, partitions=parts,
                        cylinders=geometry.cylinders_for(total))
        buffer = io.BytesIO(table.to_bytes())
        back = rdb.Rdb.read(buffer)
        self.assertEqual([p.drive_name for p in back.partitions], ["DH0", "DH1"])
        self.assertEqual(back.partitions[0].dostype, rdb.DOSTYPE_FFS_INTL)
        self.assertEqual(back.partitions[1].dostype, rdb.DOSTYPE_PFS3)
        self.assertTrue(back.partitions[0].bootable)
        self.assertFalse(back.partitions[1].bootable)
        self.assertEqual(back.partitions[0].low_cyl, parts[0].low_cyl)
        self.assertEqual(back.partitions[1].high_cyl, parts[1].high_cyl)

    def test_partitions_do_not_overlap_or_leave_gaps(self):
        geometry = rdb.Geometry()
        total = (7 * GIB) // 512
        parts = rdb.layout(geometry, total, [
            ("DH0", 1 * GIB, rdb.DOSTYPE_FFS_INTL),
            ("DH1", 2 * GIB, rdb.DOSTYPE_PFS3),
            ("DH2", None, rdb.DOSTYPE_PFS3),
        ])
        for first, second in zip(parts, parts[1:]):
            self.assertEqual(second.low_cyl, first.high_cyl + 1)
        self.assertLessEqual(parts[-1].high_cyl, geometry.cylinders_for(total) - 1)

    def test_checksums_validate(self):
        table = rdb.Rdb(partitions=[rdb.Partition("DH0", 1, 100)], cylinders=200)
        data = table.to_bytes()
        for offset in (0, 512):
            block = data[offset:offset + 512]
            summed = struct.unpack_from(">I", block, 4)[0]
            total = sum(struct.unpack_from(">I", block, i * 4)[0]
                        for i in range(summed)) & 0xFFFFFFFF
            self.assertEqual(total, 0, f"block at {offset} has a bad checksum")

    def test_fshd_seglist_pointer_is_at_offset_72(self):
        """fhb_SegListBlocks, not fhb_Startup, points at the first LoadSegBlock.

        Getting this wrong is invisible to a round trip - reader and writer agree
        with each other - but produces an RDB whose file system handler AmigaOS
        can never find.  Verified against a real HstWB image.
        """
        table = rdb.Rdb(partitions=[rdb.Partition("DH0", 1, 100)], cylinders=200,
                        filesystems=[rdb.FileSystem(rdb.DOSTYPE_PDS3, b"HUNK" * 500)])
        data = table.to_bytes()
        fshd_block = next(i for i in range(16)
                          if data[i * 512:i * 512 + 4] == rdb.ID_FSHD)
        block = data[fshd_block * 512:(fshd_block + 1) * 512]
        self.assertEqual(struct.unpack_from(">i", block, 68)[0], 0,
                         "fhb_Startup must be zero")
        pointer = struct.unpack_from(">i", block, 72)[0]
        self.assertGreater(pointer, 0)
        self.assertEqual(data[pointer * 512:pointer * 512 + 4], rdb.ID_LSEG,
                         "fhb_SegListBlocks must point at an LSEG block")

    def test_dostype_parsing(self):
        self.assertEqual(rdb.parse_dostype("PFS3"), rdb.DOSTYPE_PFS3)
        self.assertEqual(rdb.parse_dostype("DOS\\3"), rdb.DOSTYPE_FFS_INTL)
        self.assertEqual(rdb.dostype_name(rdb.DOSTYPE_PFS3), "PFS3")

    def test_embedded_filesystem_round_trip(self):
        binary = os.urandom(5000)
        table = rdb.Rdb(partitions=[rdb.Partition("DH0", 1, 100)], cylinders=200,
                        filesystems=[rdb.FileSystem(rdb.DOSTYPE_PFS3, binary)])
        back = rdb.Rdb.read(io.BytesIO(table.to_bytes()))
        self.assertEqual(len(back.filesystems), 1)
        self.assertEqual(back.filesystems[0].dostype, rdb.DOSTYPE_PFS3)
        self.assertTrue(back.filesystems[0].seglist.startswith(binary))


class TestBootConfig(_Scratch):
    TEMPLATE = (
        "# sample\n"
        "kernel=Emu68-old\n"
        "total_mem=2048\n"
        "hdmi_group=2\n"
        "hdmi_mode=82\n"
        "#force_turbo=1\n"
        "#initramfs firmware.bin.gz,DiagROM\n"
        "initramfs kick.rom\n"
    )

    def test_untouched_keys_are_preserved(self):
        config = bootcfg.ConfigTxt(self.TEMPLATE)
        bootcfg.BootOptions(kernel="Emu68-new").apply_config(config)
        text = config.text()
        self.assertIn("total_mem=2048", text)
        self.assertIn("kernel=Emu68-new", text)
        self.assertNotIn("Emu68-old", text)

    def test_only_one_initramfs_line_stays_live(self):
        config = bootcfg.ConfigTxt(self.TEMPLATE)
        bootcfg.BootOptions(kickstart_file="kick31.rom").apply_config(config)
        live = [l for l in config.text().splitlines() if l.startswith("initramfs")]
        self.assertEqual(live, ["initramfs kick31.rom"])

    def test_automatic_hdmi_comments_the_mode_out(self):
        config = bootcfg.ConfigTxt(self.TEMPLATE)
        bootcfg.BootOptions(hdmi_automatic=True).apply_config(config)
        live = [l for l in config.text().splitlines()
                if l.startswith("hdmi_group") or l.startswith("hdmi_mode")]
        self.assertEqual(live, [])

    def test_no_kickstart_disables_maprom(self):
        config = bootcfg.ConfigTxt(self.TEMPLATE)
        bootcfg.BootOptions(kickstart_file=None).apply_config(config)
        live = [l for l in config.text().splitlines() if l.startswith("initramfs")]
        self.assertEqual(live, [])

    def test_the_ocs_timing_brakes_are_emitted(self):
        """Emu68's own parser takes chip_slowdown, dbf_slowdown and blitwait.

        A PiStorm runs the 68k far faster than any real Amiga, so OCS-era
        software that times itself against the hardware breaks on speed alone.
        These are the three brakes Emu68 offers for it.
        """
        options = bootcfg.BootOptions(chip_slowdown=True, dbf_slowdown=True,
                                      blitwait=True)
        parts = options.cmdline().split()
        self.assertIn("chip_slowdown", parts)
        self.assertIn("dbf_slowdown", parts)
        self.assertIn("blitwait", parts)

    def test_the_timing_brakes_are_off_unless_asked_for(self):
        parts = bootcfg.BootOptions().cmdline().split()
        for token in ("chip_slowdown", "dbf_slowdown", "blitwait"):
            self.assertNotIn(token, parts)

    def test_cmdline_generation(self):
        options = bootcfg.BootOptions(vc4_mem=64, vbr_move=True, sd_unit0_rw=True,
                                      extra_cmdline="sd.verbose=1")
        self.assertEqual(options.cmdline(),
                         "vc4.mem=64 vbr_move sd.unit0=rw sd.verbose=1")


class TestKickstart(_Scratch):
    @staticmethod
    def rom(version=40, revision=68) -> bytes:
        data = bytearray(512 * 1024)
        data[0:4] = b"\x11\x11\x4e\xf9"
        struct.pack_into(">HH", data, 12, version, revision)
        return bytes(data)

    def test_identifies_a1200_rom(self):
        path = self.scratch() / "kick.rom"
        path.write_bytes(self.rom())
        info = kickstart.identify(path)
        self.assertTrue(info.aga)
        self.assertTrue(info.usable)
        self.assertIn("3.1", info.name)

    def test_flags_non_aga_rom(self):
        path = self.scratch() / "kick.rom"
        path.write_bytes(self.rom(40, 63))
        self.assertFalse(kickstart.identify(path).aga)

    def test_byte_swapped_rom_is_corrected(self):
        original = self.rom()
        swapped = bytearray(original)
        swapped[0::2], swapped[1::2] = original[1::2], original[0::2]
        path = self.scratch() / "kick.rom"
        path.write_bytes(bytes(swapped))
        info = kickstart.identify(path)
        self.assertTrue(info.byte_swapped)
        self.assertEqual(kickstart.prepare(info), original)

    def test_cloanto_rom_needs_its_key(self):
        folder = self.scratch()
        key = b"\x11\x22\x33\x44\x55"
        plain = self.rom()
        encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(plain))
        (folder / "kick.rom").write_bytes(b"AMIROMTYPE1" + encrypted)
        info = kickstart.identify(folder / "kick.rom")
        self.assertTrue(info.encrypted)
        self.assertFalse(info.usable)          # no key yet
        (folder / "rom.key").write_bytes(key)
        info = kickstart.identify(folder / "kick.rom")
        self.assertTrue(info.usable)
        self.assertEqual(kickstart.prepare(info), plain)


class TestJobs(_Scratch):
    def test_round_trip(self):
        config = builder.BuildConfig(
            mode=builder.BuildMode.IMAGE, target="/dev/sdz", target_is_device=True,
            source_image="/x/pimiga.img.xz",
            boot_options=bootcfg.BootOptions(hdmi_group=2, hdmi_mode=82, vbr_move=True),
            amiga_partitions=[builder.AmigaPartitionSpec("DH0", 2 * GIB, "PFS3", True, 0)])
        path = self.scratch() / "job.json"
        jobs.save(config, path)
        self.assertEqual(jobs.load(path), config)


class TestValidation(_Scratch):
    def test_rejects_two_flexible_partitions(self):
        config = builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target="/tmp/x.img",
            amiga_partitions=[builder.AmigaPartitionSpec("DH0", None),
                              builder.AmigaPartitionSpec("DH1", None)])
        self.assertTrue(any("remaining space" in p for p in config.validate()))

    def test_rejects_image_mode_without_a_source(self):
        config = builder.BuildConfig(mode=builder.BuildMode.IMAGE, target="/tmp/x.img")
        self.assertTrue(any("source image" in p.lower() for p in config.validate()))

    def test_rejects_wifi_without_password(self):
        config = builder.BuildConfig(target="/tmp/x.img", wifi_ssid="net")
        self.assertTrue(any("WiFi" in p for p in config.validate()))


class TestFullBuild(_Scratch):
    """Build a real image end to end and read the result back."""

    def test_fresh_build_produces_a_valid_card(self):
        folder = self.scratch()
        target = folder / "card.img"
        emu68_dir = folder / "emu68"
        emu68_dir.mkdir()
        (emu68_dir / "Emu68-pistorm32lite").write_bytes(os.urandom(300_000))
        (emu68_dir / "start4.elf").write_bytes(os.urandom(50_000))
        (emu68_dir / "config.txt").write_text("kernel=placeholder\ngpu_mem=64\n")
        rom = folder / "kick.rom"
        rom.write_bytes(TestKickstart.rom())

        config = builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(target),
            image_size=2 * GIB, boot_size=128 * MIB,
            emu68_prepared_dir=str(emu68_dir),
            kickstart_path=str(rom),
            amiga_partitions=[
                builder.AmigaPartitionSpec("DH0", 512 * MIB, "FFS-INTL", True, 0),
                builder.AmigaPartitionSpec("DH1", None, "PFS3", False, -128)],
            boot_options=bootcfg.BootOptions(hdmi_group=2, hdmi_mode=82, vc4_mem=32),
            wifi_ssid="Amiga", wifi_password="password1",
        )
        builder.run_build(config, QUIET)

        with open(target, "rb") as handle:
            parts = mbr.read_table(handle)
            self.assertEqual(parts[0].type_id, mbr.TYPE_FAT32_LBA)
            self.assertEqual(parts[1].type_id, mbr.TYPE_AMIGA)

            fs = fat32.Fat32(handle, parts[0].start_bytes)
            names = {e.name for e in fs.listdir("/")}
            self.assertIn("Emu68-pistorm32lite", names)
            self.assertIn("kick.rom", names)
            self.assertIn("wpa_supplicant.conf", names)
            text = fs.read_bytes("config.txt").decode()
            self.assertIn("kernel=Emu68-pistorm32lite", text)
            self.assertIn("initramfs kick.rom", text)
            self.assertIn("gpu_mem=64", text)          # upstream value preserved
            self.assertEqual(fs.read_bytes("cmdline.txt").decode().strip(),
                             "vc4.mem=32")
            self.assertEqual(fs.read_bytes("kick.rom"), rom.read_bytes())

            table = rdb.Rdb.read(handle, parts[1].start_bytes)
            self.assertEqual([p.drive_name for p in table.partitions], ["DH0", "DH1"])
            self.assertTrue(table.partitions[0].bootable)

    def test_image_mode_writes_and_then_expands(self):
        folder = self.scratch()
        source = folder / "source.img"
        emu68_dir = folder / "emu68"
        emu68_dir.mkdir()
        (emu68_dir / "Emu68-pistorm").write_bytes(os.urandom(100_000))
        (emu68_dir / "config.txt").write_text("kernel=placeholder\n")

        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(source),
            image_size=512 * MIB, boot_size=96 * MIB,
            emu68_prepared_dir=str(emu68_dir),
            amiga_partitions=[builder.AmigaPartitionSpec("DH0", None, "PFS3", True, 0)],
        ), QUIET)

        target = folder / "target.img"
        with open(target, "wb") as handle:
            handle.truncate(2 * GIB)

        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.IMAGE, target=str(target),
            source_image=str(source), emu68_prepared_dir=str(emu68_dir),
            boot_options=bootcfg.BootOptions(hdmi_automatic=True),
            expand_to_fill=True,
            extra_partitions=[builder.AmigaPartitionSpec("DH1", None, "PFS3",
                                                         False, -128)],
        ), QUIET)

        with open(target, "rb") as handle:
            parts = mbr.read_table(handle)
            amiga = next(p for p in parts if p.type_id == mbr.TYPE_AMIGA)
            #  The 0x76 partition should now reach the end of the 2 GiB target.
            self.assertGreater(amiga.size_bytes, 1800 * MIB)
            table = rdb.Rdb.read(handle, amiga.start_bytes)
            self.assertEqual([p.drive_name for p in table.partitions], ["DH0", "DH1"])
            #  The pre-existing partition must be untouched.
            self.assertTrue(table.partitions[0].bootable)
            self.assertGreater(table.partitions[1].size_bytes(table.geometry), 1 * GIB)

    @staticmethod
    def make_hdf(path: Path, size: int, *, driver: bytes = b"") -> rdb.Rdb:
        """Write a small Amiga hard disk image with an RDB at block 0."""
        geometry = rdb.Geometry()
        blocks = size // 512
        parts = rdb.layout(geometry, blocks, [("DH0", None, rdb.DOSTYPE_PDS3)])
        filesystems = [rdb.FileSystem(rdb.DOSTYPE_PDS3, driver)] if driver else []
        table = rdb.Rdb(geometry=geometry, partitions=parts, filesystems=filesystems,
                        cylinders=geometry.cylinders_for(blocks))
        with open(path, "wb") as handle:
            handle.truncate(size)
            table.write(handle, 0)
        return table

    def test_hdf_import_builds_a_card_around_the_image(self):
        folder = self.scratch()
        hdf = folder / "disk.hdf"
        self.make_hdf(hdf, 200 * MIB)
        emu68_dir = folder / "emu68"
        emu68_dir.mkdir()
        (emu68_dir / "Emu68-pistorm32lite").write_bytes(os.urandom(50_000))
        (emu68_dir / "config.txt").write_text("kernel=placeholder\n")

        target = folder / "card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.HDF, target=str(target),
            image_size=512 * MIB, boot_size=96 * MIB,
            hdf_image=str(hdf), emu68_prepared_dir=str(emu68_dir),
        ), QUIET)

        with open(target, "rb") as handle:
            parts = mbr.read_table(handle)
            boot = next(p for p in parts if p.type_id == mbr.TYPE_FAT32_LBA)
            amiga = next(p for p in parts if p.type_id == mbr.TYPE_AMIGA)
            #  Emu68 must be on the boot partition we built around the image.
            fs = fat32.Fat32(handle, boot.start_bytes)
            self.assertIn("Emu68-pistorm32lite", {e.name for e in fs.listdir("/")})
            #  and the image's own RDB must be readable at the 0x76 partition.
            table = rdb.Rdb.read(handle, amiga.start_bytes)
            self.assertEqual([p.drive_name for p in table.partitions], ["DH0"])
            self.assertEqual(table.partitions[0].dostype, rdb.DOSTYPE_PDS3)

    def test_bare_hdf_without_an_rdb_is_wrapped(self):
        """A single-partition image gets an RDB built around it.

        ClassicWB's System_*.hdf files are a bare FFS file system with no
        partition table; Emu68 looks for an RDB at the start of the 0x76
        partition, so the image has to be offset and described.
        """
        folder = self.scratch()
        hdf = folder / "bare.hdf"
        blocks = 2048 * 100                       # exactly 100 cylinders at 16x128
        payload = bytearray(blocks * 512)
        payload[0:4] = b"DOS\x01"
        marker_bytes = os.urandom(64)
        payload[512:512 + 64] = marker_bytes      # something to find later
        hdf.write_bytes(bytes(payload))

        info = builder.inspect_hdf(hdf)
        self.assertIsNone(info.table)
        self.assertEqual(info.bare_dostype, rdb.DOSTYPE_FFS)

        target = folder / "card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.HDF, target=str(target),
            image_size=512 * MIB, boot_size=96 * MIB,
            hdf_image=str(hdf), install_emu68=False,
            amiga_partitions=[builder.AmigaPartitionSpec("DH0", None, "FFS", True, 0)],
        ), QUIET)

        with open(target, "rb") as handle:
            amiga = next(p for p in mbr.read_table(handle)
                         if p.type_id == mbr.TYPE_AMIGA)
            table = rdb.Rdb.read(handle, amiga.start_bytes)
            self.assertEqual(len(table.partitions), 1)
            part = table.partitions[0]
            self.assertEqual(part.dostype, rdb.DOSTYPE_FFS)
            self.assertTrue(part.bootable)
            #  The declared partition must be exactly as big as the image, or
            #  the file system's bitmap would not cover the whole partition.
            self.assertEqual(part.blocks(table.geometry), blocks)
            #  ...and the image data must start exactly at the partition start.
            handle.seek(amiga.start_bytes + part.start_block(table.geometry) * 512)
            self.assertEqual(handle.read(4), b"DOS\x01")
            handle.seek(amiga.start_bytes
                        + part.start_block(table.geometry) * 512 + 512)
            self.assertEqual(handle.read(64), marker_bytes)

    def test_geometry_is_chosen_to_divide_the_image_exactly(self):
        for blocks in (1024000, 1024001, 999999, 2048, 63):
            geometry = builder.geometry_dividing(blocks)
            self.assertEqual(blocks % geometry.cyl_blocks, 0, f"{blocks} blocks")

    def test_hdf_too_big_for_the_card_is_refused(self):
        folder = self.scratch()
        hdf = folder / "disk.hdf"
        self.make_hdf(hdf, 400 * MIB)
        target = folder / "card.img"
        with self.assertRaises(RuntimeError) as caught:
            builder.run_build(builder.BuildConfig(
                mode=builder.BuildMode.HDF, target=str(target),
                image_size=300 * MIB, boot_size=96 * MIB,
                hdf_image=str(hdf), install_emu68=False), QUIET)
        self.assertIn("Amiga partition is only", str(caught.exception))

    def test_filesystem_driver_is_lifted_from_a_donor_image(self):
        """A PFS3 handler can be taken out of an existing .hdf's RDB."""
        folder = self.scratch()
        donor = folder / "donor.hdf"
        driver = b"\x00\x00\x03\xf3" + os.urandom(9_000)
        self.make_hdf(donor, 200 * MIB, driver=driver)

        target = folder / "card.img"
        builder.run_build(builder.BuildConfig(
            mode=builder.BuildMode.FRESH, target=str(target),
            image_size=512 * MIB, boot_size=96 * MIB, install_emu68=False,
            pfs3_binary=str(donor),
            amiga_partitions=[builder.AmigaPartitionSpec("DH0", None, "PDS3", True, 0)],
        ), QUIET)

        with open(target, "rb") as handle:
            amiga = next(p for p in mbr.read_table(handle)
                         if p.type_id == mbr.TYPE_AMIGA)
            table = rdb.Rdb.read(handle, amiga.start_bytes)
        self.assertEqual(len(table.filesystems), 1)
        self.assertEqual(table.filesystems[0].dostype, rdb.DOSTYPE_PDS3)
        self.assertTrue(table.filesystems[0].seglist.startswith(driver))

    def test_image_larger_than_target_is_refused(self):
        folder = self.scratch()
        source = folder / "big.img"
        with open(source, "wb") as handle:
            handle.truncate(1 * GIB)
        target = folder / "small.img"
        with open(target, "wb") as handle:
            handle.truncate(256 * MIB)
        config = builder.BuildConfig(mode=builder.BuildMode.IMAGE, target=str(target),
                                     source_image=str(source), install_emu68=False)
        with self.assertRaises(RuntimeError) as caught:
            builder.run_build(config, QUIET)
        self.assertIn("but the target is only", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
