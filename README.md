# PiStorm Imager for Linux

A GTK4 desktop application for preparing PiStorm / Emu68 SD cards on Linux.

The official [Emu68 Imager](https://mja65.github.io/Emu68-Imager/) is Windows and
PowerShell only. This is a native Linux replacement, and it additionally
understands pre-built images such as **PiMiga**, so you can write one to a card
and still apply your own Emu68 build, Kickstart, video mode and WiFi settings on
top of it.

## What it does

Three tasks, all ending with the same boot-partition customisation pass:

| Task | What happens |
| --- | --- |
| **Build a new card** | Writes an MBR with a FAT32 boot partition (Emu68 + Raspberry Pi firmware + your Kickstart) and a type `0x76` Amiga partition carrying a Rigid Disk Block. Optionally installs AmigaOS onto it from a set of Workbench floppy images, so the card boots straight to Workbench. |
| **Write a pre-built image** | Streams PiMiga, an Emu68 Hatcher image or a backup of your own card onto the target, then re-applies your Emu68 build and settings. Optionally turns the card's leftover space into a new Amiga partition. |
| **Import an Amiga hard disk image** | Takes a WinUAE/FS-UAE/HstWB `.hdf` — the Amiga drive on its own, with no partition table — and builds the boot partition around it. Images with no Rigid Disk Block get one generated for them, and a whole card image such as PiMiga can be used here too: only its Amiga drive is taken, so it can be moved onto a card of a different size with a fresh boot partition. Every imported drive is checked for PiStorm compatibility and repaired. |

It can also produce a bare **Amiga hard disk image** instead of a card, which
works here and in WinUAE or FS-UAE.
| **Update an existing card** | Touches only the boot partition: swap the Emu68 version, change the Kickstart, alter the HDMI mode, add WiFi. Everything on the Amiga side is left alone. |

Along the way it will:

* download the right Emu68 release for your board, **including the 1.1 asset
  rename** (`Emu68-pistorm.zip` meant the classic PiStorm up to 1.0.7 and means
  the FPGA boards from 1.1 onwards), and fetch the Raspberry Pi boot firmware
  separately for releases that no longer bundle it;
* identify Kickstart ROMs by looking *inside* them rather than by file name,
  warn when a ROM is not an A1200/AGA one, decrypt Cloanto `AMIROMTYPE1` ROMs
  when `rom.key` is available, and silently correct byte-swapped dumps;
* edit the `config.txt` that ships with your chosen Emu68 release rather than
  generating a new one, so upstream's comments and per-release tuning survive
  and only the keys you actually set are changed;
* write `cmdline.txt` from the documented Emu68 options (`vc4.mem`, `vbr_move`,
  `chip_slowdown`, `sd.unit0=rw`, and anything else you type in);
* install AmigaOS from ADFs, recognising each disk by the **volume name inside
  it** rather than its file name, and keeping the whole set to one release (a
  2.0 Extras drawer on a 3.1 system is a broken install, and collections
  routinely hold several releases side by side).

## Requirements

Everything is either in the Python standard library or already on a normal
GNOME desktop:

```
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 dosfstools
```

`p7zip-full` is only needed for `.7z`/`.rar` source images. There is no
`mtools`, `amitools` or `parted` dependency: the FAT32 and RDB layers are
implemented in this project.

## Running it

```
./run.sh
```

or install it and use the desktop entry:

```
pip install --user .
cp pistorm-imager.desktop ~/.local/share/applications/
```

## Privileges

The interface never runs as root. All downloading and unpacking happens as you;
when the target is a real SD card the writing step alone is re-executed through
`pkexec`, which means the privileged half is offline and only touches the card.
Writing to an `.img` file needs no authentication at all.

The card chooser lists only removable drives, and a device currently providing
`/`, `/home`, `/boot` and friends is refused outright.

## Command line

The same engine without the GUI:

```
python3 -m pistorm_imager.cli list-devices
python3 -m pistorm_imager.cli releases
python3 -m pistorm_imager.cli identify ~/Kickstarts/*.rom
python3 -m pistorm_imager.cli inspect /dev/mmcblk0        # partitions + RDB
python3 -m pistorm_imager.cli check disk.hdf --fix        # compatibility repair
python3 -m pistorm_imager.cli build --job saved-settings.json
```

`Save settings…` in the GUI menu writes exactly the job file that `build`
consumes, so a card can be reproduced later or on another machine.

## Layout

```
pistorm_imager/
  core/
    fat32.py     FAT32 reader/writer working directly on an image or device
    amigafs.py   Amiga OFS/FFS: reads ADFs, creates and fills FFS partitions
    amigaos.py   recognising Workbench disks and installing them
    mbr.py       DOS partition table
    rdb.py       Amiga Rigid Disk Block: partitions and embedded file systems
    emu68.py     GitHub releases, asset naming, Raspberry Pi firmware
    kickstart.py ROM identification, Cloanto decryption, byte-swap repair
    bootcfg.py   config.txt / cmdline.txt editing
    imgsrc.py    streaming readers for .img/.xz/.gz/.zip/.7z sources
    hdfcheck.py  PiStorm compatibility analysis and RDB repair
    pfs3.py      PFS3: reads real volumes, creates and fills new ones
    compat.py    automatic emulator-to-PiStorm fixes (RTG driver, startup)
    amigainfo.py Workbench .info icons, enough to retarget tool types
    builder.py   the orchestrator
    jobs.py      job serialisation across the privilege boundary
  ui/            the GTK4 interface
  cli.py         command line front end and privileged writer
tests/           unit tests plus a real end-to-end image build
```

## Tests

```
python3 -m unittest discover -s tests -p 'test_*.py' -v   # 92 tests
python3 tests/test_gui_smoke.py                           # needs a display
```

The core suite builds real images in a temporary directory and reads them back,
validates the FAT32 output with `fsck.vfat`, and audits the FFS bitmap to prove
no block that is in use is ever marked free. The file system tests run against
the real Workbench 3.1 disks in `samples/` when they are present.

Both the ADF reader and the FFS writer were cross-checked against
[amitools](https://github.com/cnvogelg/amitools): every one of the 153 files on
the Workbench 3.1 disk extracts byte-identically to `xdftool`, and volumes
written here read back correctly in `xdftool`. That independent check matters -
a reader and writer that share a mistake agree with each other perfectly.

## Status

Working and tested end to end: partitioning, FAT32 creation and population,
Emu68 installation, Kickstart handling, `config.txt`/`cmdline.txt`, RDB
creation, image writing (including compressed sources), and expansion into
unused space.

Validated against real material: a full Workbench 3.1 install built from the
original floppy images (643 files, verified in `xdftool`), a 106 GiB HstWB
`120gb.hdf` (its RDB, its PFS3 19.2 and FFS 45.13 handlers), the 500 MiB
ClassicWB `System_P96.hdf` (wrapped in a generated RDB), and a collection of
about 100 Kickstart ROMs including Cloanto-encrypted ones.

**Not yet verified:** none of this has been booted on real PiStorm hardware.
Every structure is checked against the format specifications and against
independent tools, but that is not the same as an Amiga booting from it.

## Installing AmigaOS from floppy images

Point the tool at a folder of ADFs and it identifies them by volume name -
`Workbench3.1`, `Extras3.1`, `Fonts`, `Locale`, `Storage3.1`, `Install3.1` -
picking the best dump of each (a verified GoodTools `[!]` image is preferred
over a modified one) and refusing to mix releases. The layout it produces
follows the AmigaOS install script:

| Disk | Goes to |
| --- | --- |
| Workbench | the root of the drive |
| Extras | the root of the drive, without overwriting Workbench's files |
| Fonts / Locale / Storage / Backdrops / Classes | drawers of those names |
| Install | its own `Install` drawer, so its cut-down `C/`, `L/` and `Libs/` cannot replace the full versions |

File contents, protection bits, comments and datestamps are all carried across
unchanged. The partition being installed onto must be FFS, because that is the
only Amiga file system this tool can create.

Put your own Workbench disks and Kickstart in `samples/` and they are found
automatically - see `samples/README.md`. None of that material is kept in this
repository, and the tests that use it skip when it is absent.

## Checking and repairing an imported drive

Most `.hdf` files were built for WinUAE, which is forgiving about things real
hardware is not. Every imported drive is analysed, and the safe repairs applied
automatically — only RDB metadata is rewritten, never partition contents, so a
repair cannot lose files.

What it looks for, and fixes where it can:

* **MaxTransfer above `0x1FE00`** — the classic cause of silent data corruption
  on real hardware, and very common in images built for emulators.
* **A transfer Mask that allows odd addresses.**
* A partition whose **file system handler is not in the RDB** and is not one
  Kickstart provides. PFS3 is the usual case; the handler can be lifted out of
  any other image that has one, and a PFS3 and PDS3 handler are the same binary,
  so either satisfies a partition asking for the other.
* **No partition marked bootable**, **duplicate device names**, a
  `SectorsPerBlock` other than 1, zero reserved blocks or zero buffers.
* **Overlapping partitions, partitions past the end of the drive, a partition
  sitting on the RDB, a non-512-byte block size** — reported and refused, since
  fixing them would mean moving or reformatting data.

```
python3 -m pistorm_imager.cli check disk.hdf
python3 -m pistorm_imager.cli check disk.hdf --fix --donor pimiga.img
```

A missing handler is reported as an error but does not stop a build: the drive
itself is fine, and the handler can equally be added later from HDToolBox.

## PFS3

Partitions can be created and filled as **PFS3** as well as FFS. That matters
because Kickstart's FFS is slow and unreliable much past a couple of gigabytes,
while an SD card invites partitions far larger than that.

PFS3 is not part of Kickstart, so its handler must also be embedded in the RDB
or the Amiga cannot mount the partition. Point the tool at a `pfs3aio` binary,
**or at another `.hdf` that already contains one** — an HstWB or PiMiga image
carries a matching PFS3, and the handler is lifted straight out of its RDB. A
PFS3 and a PDS3 handler are the same binary, so either satisfies a partition
asking for the other. FFS partitions need no driver.

The PFS3 implementation was written from the on-disk format in
[`tonioni/pfsdoctor`](https://github.com/tonioni/pfsdoctor) and the reference
implementation in [`tonioni/pfs3aio`](https://github.com/tonioni/pfs3aio), then
checked both ways: the reader against three real PFS3 volumes (in both the
small-index and SUPERINDEX layouts), and volumes written here against
[`metaneutrons/pfs3`](https://github.com/metaneutrons/pfs3), an independent
Rust implementation, which reports them clean and extracts their files
byte-identically.

## Bringing an emulator installation to real hardware

A system built for Amiberry or WinUAE is ordinary Amiga software — AmigaOS 3.9,
Scalos and a Kickstart ROM all behave the same on a PiStorm. What does not carry
over is the *emulator's own drivers*, and a graphics driver for a card that does
not exist leaves Workbench with nowhere to appear.

Give a partition a **content folder** — a directory-based drive from an emulator,
such as PiMiga's `disks/System` — and it is copied into a real Amiga partition
with these fixes applied automatically:

* the emulator's RTG driver (`uaegfx.card`) is dropped and Emu68's
  `VideoCore.card` installed in `LIBS:Picasso96/` in its place;
* the Picasso96 monitor icon in `DEVS:Monitors` is rewritten with
  `BOARDTYPE=VideoCore`, which is how Picasso96 chooses its board;
* startup scripts have emulator-only commands (`uae-configuration` and friends)
  commented out, so they cannot fail the boot.

Every change is reported in the log, and none of them touch your files. Turn the
whole thing off with `fix_compatibility=False` if you would rather do it by hand.

## Hard disk images: two shapes

A `.hdf` is not the same thing as a card image, and the difference decides
whether it boots:

* **With an RDB** (HstWB's `120gb.hdf`, most multi-partition HDFs) — block 0
  starts with `RDSK` and already describes DH0, DH1 and so on. It is written
  into the 0x76 partition unchanged.
* **Without an RDB** (ClassicWB's `System_P96.hdf`, most single-partition HDFs)
  — block 0 starts with a bare file system signature such as `DOS\1`. The image
  is moved past a generated RDB, and the drive geometry is chosen so that a
  whole number of cylinders matches the image *exactly*: the file system's
  bitmap covers precisely the blocks in the file, and a partition rounded up to
  the next cylinder would leave AmigaOS believing in blocks the file system
  knows nothing about.

Check what you have before writing a card:

```
python3 -m pistorm_imager.cli inspect /path/to/disk.hdf
```
