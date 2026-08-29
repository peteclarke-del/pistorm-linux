# PiStorm Imager for Linux

A GTK4 desktop application for preparing PiStorm / Emu68 SD cards on Linux.

The official [Emu68 Imager](https://mja65.github.io/Emu68-Imager/) is Windows and
PowerShell only. This is a native Linux replacement, and it additionally
understands pre-built images such as **PiMiga**, so you can write one to a card
and still apply your own Emu68 build, Kickstart, video mode and WiFi settings on
top of it.

## What it does

Four ways to end up with a card, all finishing with the same boot-partition pass:

| Task | What happens |
| --- | --- |
| **Build a new card** | Writes an MBR with a FAT32 boot partition (Emu68 + Raspberry Pi firmware + your Kickstart) and a type `0x76` Amiga partition carrying a Rigid Disk Block. Optionally installs AmigaOS onto it from Workbench floppy images, so the card boots straight to Workbench. |
| **Write a pre-built image** | Streams PiMiga, an Emu68 Hatcher image or a backup of your own card onto the target, then re-applies your Emu68 build and settings. Can turn the card's leftover space into further Amiga partitions. |
| **Import an Amiga hard disk image** | Reads a WinUAE/FS-UAE/HstWB `.hdf` and builds the card around it, copying its *contents* rather than its blocks, so its partition scheme is reproduced and its graphics driver adapted on the way in. Images with no Rigid Disk Block get one generated. A whole card image such as PiMiga works here too: only its Amiga drive is taken, so it can move onto a card of a different size. |
| **Update an existing card** | Touches only the boot partition: swap the Emu68 version, change the Kickstart, alter the HDMI mode, add WiFi. Everything on the Amiga side is left alone. |

It can also produce a bare **Amiga hard disk image** instead of a card, which
works here and in WinUAE or FS-UAE.

## Quick setup

The first page asks what hardware you have and works the rest out. Almost
everything on a card is the same whatever Amiga it goes into; the model decides
only a handful of things:

| From the model | From the display |
| --- | --- |
| Which Emu68 build to download | Whether an RTG driver is installed |
| Which Kickstart suits the machine | Whether an HDMI mode is forced |
| `chip_slowdown` for OCS/ECS | `unicam` for a Framethrower |
| The slow-RAM options | How much video memory P96 is given |
| Which chipset-specific games are worth copying | |

Supported models: **A500, A500+, A600, A1000, A2000, A1200**, and a bare
Raspberry Pi. `vbr_move` is never enabled by default on any of them - it is
faster, but Emu68's own documentation says it badly hurts floppy-loaded games
and demos, which is the wrong trade on machines that exist to run them.

The Kickstart, the Workbench disks and the PFS3 handler are found on their own
if they are in `samples/`, so a typical setup is: pick the model, pick how you
look at it, choose where to write, and read the plan.

## Sizes: GB and GiB are not the same

A card sold as 32 GB holds 29.8 GiB, so an image built as "32 GiB" is over two
gigabytes too big for it. Sizes therefore distinguish the two:

| You type | You get |
| --- | --- |
| `32GB` | 29.80 GiB - fits a 32 GB card |
| `32GiB` | 32.00 GiB |
| `32G` | 32.00 GiB, binary by the usual convention |

The size is shown in both readings as you type, along with the capacity of card
an image will need. When writing to a card the field is not used at all: the
real device size is.

## Saved settings

The setup is remembered between runs - in `~/.config/pistorm-imager/session.json`
- and restored when the application starts. It is saved when a quick setup is
applied, when a build finishes, and when the window closes. **Forget saved
setup** in the menu discards it, and settings can also be saved to and loaded
from a file of your choosing.

A saved file records the build *and* the choices behind it: which Amiga, which
display, which folders. A build configuration alone cannot express those, so
saving it by itself would still leave the hardware to be picked again.

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
    machines.py  target Amiga models and what each decides
    presets.py   quick setup: finding material and laying out a card
    jobs.py      saved settings and sessions
    prepare.py   downloads done as you, before the privileged write
    builder.py   the orchestrator
    jobs.py      job serialisation across the privilege boundary
  ui/            the GTK4 interface
  cli.py         command line front end and privileged writer
tests/           unit tests plus a real end-to-end image build
```

## Tests

```
python3 -m unittest discover -s tests -p 'test_*.py' -v   # 162 tests
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
creation and repair, PFS3 and FFS volume creation, image writing (including
compressed sources), copying a drive's contents from an image or a directory,
and the automatic compatibility pass.

Validated against real material: a full Workbench 3.1 install built from the
original floppy images (643 files, verified in `xdftool`), a 106 GiB HstWB
`120gb.hdf` (its RDB, its PFS3 19.2 and FFS 45.13 handlers, and its three PFS3
partitions), the 500 MiB ClassicWB `System_P96.hdf` (copied into a PFS3
partition with its graphics driver swapped), PiMiga 5, and a collection of about
100 Kickstart ROMs including Cloanto-encrypted ones.

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

PFS3 volumes are created with the full **107-character** file name limit rather
than the conservative 32 that `pfs3aio`'s own formatter writes. That matters
because renaming a file breaks whatever refers to it by name - a WHDLoad slave,
an icon's tool types - so a games or demos drive copies with nothing renamed at
all. FFS has a hard 30-character limit and no way round it; where a name must
be shortened there, a file that has an icon is given 25 characters so the icon
still fits alongside it, and the log says which files were affected.

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

PiMiga 5 is worth a word of warning: despite the name it is **not a PiStorm
image**. It is a Debian system running the Amiberry emulator, with an ext4 root
and no `0x76` partition at all, so it cannot be written to a card and booted by
Emu68. Its Amiga drives are plain directories under `disks/`, which is what
makes the conversion below possible.

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
