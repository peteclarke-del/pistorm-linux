# PiStorm Imager for Linux

A GTK4 desktop application for preparing PiStorm / Emu68 SD cards on Linux.

The official [Emu68 Imager](https://mja65.github.io/Emu68-Imager/) is Windows and
PowerShell only. This is a native Linux replacement, and it additionally
understands pre-built images such as **PiMiga**, so you can write one to a card
and still apply your own Emu68 build, Kickstart, video mode and WiFi settings on
top of it.

## What it does

Four tasks, all ending with the same boot-partition customisation pass:

| Task | What happens |
| --- | --- |
| **Build a new card** | Writes an MBR with a FAT32 boot partition (Emu68 + Raspberry Pi firmware + your Kickstart) and a type `0x76` Amiga partition carrying a Rigid Disk Block. Optionally installs AmigaOS onto it from a set of Workbench floppy images, so the card boots straight to Workbench. |
| **Write a pre-built image** | Streams PiMiga, an Emu68 Hatcher image or a backup of your own card onto the target, then re-applies your Emu68 build and settings. Optionally turns the card's leftover space into a new Amiga partition. |
| **Import an Amiga hard disk image** | Takes a WinUAE/FS-UAE/HstWB `.hdf` — the Amiga drive on its own, with no partition table — and builds the boot partition around it. Images with no Rigid Disk Block get one generated for them, and a whole card image such as PiMiga can be used here too: only its Amiga drive is taken, so it can be moved onto a card of a different size with a fresh boot partition. Every imported drive is checked for PiStorm compatibility and repaired. |

| **Update an existing card** | Touches only the boot partition: swap the Emu68 version, change the Kickstart, alter the HDMI mode, add WiFi. Everything on the Amiga side is left alone. |

It can also produce a bare **Amiga hard disk image** instead of a card, which
works here and in WinUAE or FS-UAE.

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
    machines.py  target machine profiles: chipset, board, Kickstart, display
    presets.py   turns a machine and a source into a complete build
    packages.py  optional software taken from a system you already have
    devices.py   finding and describing removable drives
    prepare.py   partitioning and formatting the target
    util.py      sizes, progress reporting, stream copying
    builder.py   the orchestrator
    jobs.py      job serialisation across the privilege boundary
  ui/            the GTK4 interface
  cli.py         command line front end and privileged writer
tests/           unit tests plus a real end-to-end image build
```

## Tests

```
python3 -m unittest discover -s tests -p 'test_*.py' -v   # 212 tests
python3 tests/test_gui_smoke.py                           # needs a display
```

The core suite builds real images in a temporary directory and reads them back,
validates the FAT32 output with `fsck.vfat`, and audits the FFS bitmap to prove
no block that is in use is ever marked free. The file system tests run against
the real Workbench 3.1 disks in `samples/` when they are present.

PFS3 volumes are also booted in **FS-UAE**, which runs the real PFS3 19.2
handler out of the RDB rather than this project's reader: a small image with
Workbench 3.1 installed from the disks in `samples/`, booted to a
`S:User-Startup` that writes what it can see back onto the volume. That is the
only check that distinguishes a volume which is genuinely correct from one this
code merely agrees with itself about.

Both the ADF reader and the FFS writer were cross-checked against
[amitools](https://github.com/cnvogelg/amitools): every one of the 153 files on
the Workbench 3.1 disk extracts byte-identically to `xdftool`, and volumes
written here read back correctly in `xdftool`. That independent check matters -
a reader and writer that share a mistake agree with each other perfectly.

## Status

Working and tested end to end: partitioning, FAT32 creation and population,
Emu68 installation, Kickstart handling, `config.txt`/`cmdline.txt`, RDB
creation, image writing (including compressed sources), and expansion into
unused space. On top of that: PFS3 and FFS volumes created and filled, PiMiga
and `.hdf` drives imported and adapted, per-machine presets, the display
handling described above, and optional software copied from a donor system.

Validated against real material: a full Workbench 3.1 install built from the
original floppy images (643 files, verified in `xdftool`), a 106 GiB HstWB
`120gb.hdf` (its RDB, its PFS3 19.2 and FFS 45.13 handlers), the 500 MiB
ClassicWB `System_P96.hdf` (wrapped in a generated RDB), and a collection of
about 100 Kickstart ROMs including Cloanto-encrypted ones.

**Verified on hardware:** a basic Workbench-only card, built here from the
original floppy images, has been written and booted on a real PiStorm. That
covers the parts every build shares — the MBR, the FAT32 boot partition, the
Emu68 and firmware payload, `config.txt` and `cmdline.txt`, the `0x76`
partition, the Rigid Disk Block inside it and the AmigaOS install on top.

**Not yet tried on hardware:** everything past that. Importing PiMiga or an
`.hdf`; the RTG and dual-output display handling, including the switcher
scripts; optional software copied from a donor system; multi-partition layouts.
Those are checked against the format specifications and against independent
tools, which is not the same as an Amiga booting from them.

## One primary source, not several

Quick setup asks a single question about where the card's contents come from,
because the answers are alternatives rather than additions:

- **Default** - a new drive, which can then have Workbench installed onto it
  from your floppy images, or be left as bare formatted partitions.
- **PiMiga installation** - its drives, games and demos wholesale, with the
  graphics driver replaced for the target machine.
- **Amiga hard disk image** - the partition scheme and contents of an existing
  `.hdf`, again with the graphics driver adapted.

Choosing one drops the source it replaces. A PiMiga folder left behind after an
image was chosen instead used to be carried into the build, producing a card
that was neither one thing nor the other.

Adding to a source rather than replacing it is a per-partition matter: any
partition on the **Amiga partitions** page can be filled from a drive inside an
`.hdf`, so an image can be added as a fourth drive beside PiMiga's System,
Games and Work rather than displacing them.

### What Quick setup decides, and what it leaves alone

Applying a quick setup rebuilds the whole layout from the machine, the card and
the source — that is what the page is for. It has no opinion about the settings
made elsewhere, so those are carried through untouched: the WiFi network, the
volume name, the Emu68 release and any local archive, a Cloanto Kickstart key,
the source image and `.hdf` on the Source page, and the boot switches only a
person can decide (overclock, CM4 antenna, swapping `DF0:` with `DF1:`, letting
the Amiga write to the whole card). Applying used to return every one of them to
its default and then save the session in that state, so they could not be kept
at all.

The one field the two share is **Additional cmdline.txt options**: the trapdoor
switch puts `move_slow_to_chip` there and anything else in the box was typed by
hand. Both survive, and turning the switch off removes only its own option.

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

## Checking an image against the machine

Plenty of ready-made drives are built for an A1200 and say so only by the
display modes they install. The monitor drivers in `DEVS:Monitors` are checked
against the target's chipset, so importing an AGA-built system onto an A500
says:

> installs display modes this machine cannot produce: AGA (needs AGA),
> Multiscan (needs ECS). Workbench may open on a screen the OCS chipset cannot
> show.

`STORAGE:Monitors` is deliberately ignored - AmigaOS ships the whole set there
uninstalled, so its contents say nothing about what a system expects.

Chipset-specific game collections are handled separately: on a machine without
AGA, the `WHDLOAD/AGA` and `WHDLOAD/CD32` categories are simply not copied.

## Software to add to a floppy install

A Workbench built from the original disks is exactly what shipped in 1994: no
archiver, no installer, and no idea what WHDLoad is. The pieces almost everyone
adds next can be ticked on:

| | |
| --- | --- |
| **WHDLoad** | Runs floppy games and demos from the hard drive |
| **LhA** | The archiver Amiga software is distributed in |
| **Installer** | Commodore's installer, which most install scripts expect |
| **iGame** | A launcher listing WHDLoad games with screenshots |
| **Picasso96** | The RTG subsystem; only offered where there is an RTG display |

None of it is shipped with this project - it belongs to its authors - so each is
copied out of a system you already have. Point at a PiMiga folder or any
Workbench System drive and whatever is present there becomes available; the
rest is greyed out with the reason.

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

Past about 4.9 GiB a volume switches to the **SUPERINDEX** layout, and that
changes where the anode index lives: the root block's index array is given over
to the bitmap, and the handler instead reaches the index blocks through a level
of `'SB'` super blocks named by the root block extension. Getting this wrong is
silent at build time and fatal at boot — the volume looks complete, every file
is written and every index block is in place, but the handler cannot reach any
of it and refuses to mount with *Anode index invalid* followed by *Disk update
failed*. Both layouts are now created and read back in the tests; the large one
uses a sparse 5 GiB volume, which is the smallest size that turns SUPERINDEX
on.

Two more details only show up when a written volume is measured against a real
one, and both are the kind that a reader written alongside the writer will
agree with perfectly:

* **The block bitmap covers the whole partition, not the data area.** Bit *n*
  is block *n* counted from the start of the volume, so the boot block and the
  entire reserved area sit at the bottom of it, marked as taken. The handler
  works the number of bitmap blocks out from `disksize`; size the bitmap from
  the data area instead and it comes out short by however many blocks the
  reserved area occupies, which on a small volume rounds to the same number and
  on a large one does not.
* **Every directory entry ends with a two-byte "extra fields" bitmask**, because
  these volumes carry `MODE_DIR_EXTENSION`. The handler reads it by stepping
  back from the end of the entry. Leave it out and the last two bytes of the
  name are read as that bitmask instead — zero, and so harmless, for an
  even-length name, but not for an odd one.
* **Every block of a directory names that directory's parent**, not just the
  first. A directory that outgrows one block becomes a chain of them, and each
  block carries the anode of its own directory and of that directory's parent.
  Filling the parent in on the first block only is invisible to a name lookup,
  which walks the chain comparing names — but anything that has to resolve an
  object's *path* asks the block the entry sits in who its parent is, and a
  zero there reads as the root. A file in the tenth block of `LIBS:` then
  resolves to `SYS:` + its own name, which does not exist, so it can be found
  and never opened.

## Two outputs at once

A PiStorm does not take the Amiga's own video away. The chipset carries on
driving the RGB port whatever the Pi is doing, so a very common setup has both
live: Workbench on a flat panel over the Pi's HDMI, games and demos on a 1084
plugged into the Amiga. **Both — RTG on the Pi's HDMI and the Amiga's own video
output** covers that, and it is not the same as either output alone:

| | Emu68 RTG driver | Native monitor driver | Saved screen mode |
| --- | --- | --- | --- |
| The Amiga's own output | removed | installed | dropped |
| The Pi's HDMI (RTG) | installed | not needed | kept |
| Both | installed | installed | depends on the next question |
| Framethrower | installed | installed | depends on the next question |

With two outputs there is a real question — **where Workbench opens** — and the
answer changes what is written. Left on the RTG screen (the default) the saved
screen mode is kept as it is. Moved to the Amiga's own output, the saved mode is
dropped so Workbench falls back to a native one, while the RTG driver stays
installed for whatever wants it.

Native screen modes need a monitor driver to be selectable, and a system built
around an emulator's RTG board usually has nothing in `DEVS:Monitors` but that
board. Wherever the Amiga's own output is in use, the uninstalled `PAL` (or
`NTSC`) copy that AmigaOS ships in `STORAGE:Monitors` is installed, so Prefs has
something to offer. Where one is already installed, nothing is touched.

With one output there is nothing to decide, so the question is not asked, and a
preference left over from a two-output setup is ignored rather than obeyed —
honouring it would open Workbench on a screen nobody is looking at.

### Switching without rebuilding

Which monitor is actually switched on is not a property of the card. Some days
it is the HDMI panel, some days the Amiga's monitor, some days both — so with
two outputs wired the answer is *not* settled when the card is written. Two
scripts are installed:

```
Execute S:PiStorm-Use-HDMI          ; Workbench on the RTG screen
Execute S:PiStorm-Use-Amiga-Video   ; Workbench on the Amiga's own output
```

Reboot and Workbench opens where you asked. This works because AmigaOS decides
that one way: if there is a saved screen mode in `ENVARC:Sys`, Workbench opens
on the RTG board it names; if there is none, it falls back to a native mode. The
scripts move that one file in and out, stashing it in `SYS:Storage/PiStorm/`, so
nothing is lost either way and either direction can be taken as often as you
like.

The stash is filled from whatever the system already had. Nothing is fabricated:
writing a screen mode from scratch would mean guessing a Picasso96 display ID,
and a wrong guess opens Workbench on a screen that does not exist. If a card was
built with no RTG mode saved anywhere, `PiStorm-Use-HDMI` says so and tells you
to set one in Prefs/ScreenMode first — after which switching works in both
directions for good.

Every step in both scripts is guarded with `IF EXISTS`. In an AmigaDOS script a
command that fails — deleting a file that is not there, making a drawer that
already exists — stops the whole script at the default `FAILAT` of 10.

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

### Linux file names, Amiga file names

Such a drive was assembled under rules that are not the Amiga's, and the
differences have to be settled on the way in:

* **Character set.** Amiga names are ISO-8859-1 bytes, and Linux stores file
  names as bytes too, so `português.language` already carries exactly the bytes
  AmigaOS wants — even though Python cannot read them as UTF-8. Those names are
  passed through untouched. A name genuinely stored as UTF-8 is converted, and
  the occasional letter ISO-8859-1 has no room for is folded to its unaccented
  form (`čeština` → `cestina`) rather than replaced with `?`, which AmigaDOS
  reads as a pattern wildcard.
* **Case.** AmigaDOS cannot tell `Bombuzal.slave` from `Bombuzal.Slave`, and a
  collection built on Linux is full of such pairs — on PiMiga's Games drive,
  289 of them. Only one of each can exist here, and, which is what decides the
  matter, only one can be *reached*: every spelling of a name finds the same
  entry, so a second copy kept as `Bombuzal_2.slave` is a file nothing would
  ever ask for. The second copy is therefore left out, and the card holds what
  the drawer always looked like to the Amiga.

  Which one stays is decided by the drawer's icon. A WHDLoad icon names its
  slave in a `SLAVE=` tool type, and an emulator mounting the host directory
  opens that exact spelling; keeping the other would run a *different build* of
  the game here than the same collection runs there. That is not a matter of
  taking the newest file — in seven of PiMiga's pairs the icon names a slave
  years older than the one beside it, and reproducing what it does means
  keeping the old one. Every file left out is named in the log.

  Two drawers of the same name are **merged into one** rather than either being
  renamed, which would leave a game looking for half of its files.
* **Length.** FFS allows 30 characters, PFS3 far more. Only a name that really
  had to be cut short is reported as shortened, and that is the warning worth
  acting on: a shortened name can stop a game starting, because a WHDLoad slave
  and an icon's tool types both name files. Where the reference is one this tool
  can see — a tool type naming a file in the same drawer — it is **rewritten to
  match**, so the icon still launches its slave. A name buried inside a binary
  cannot be reached that way, which is why the warning still exists. Choosing
  PFS3 sidesteps the question almost entirely: 3 names across PiMiga's four
  drives are too long for it, against 1,597 on the Work drive alone under FFS.

Across PiMiga 5's System, Demos and Games drives this brings the names that have
to change down from 309 to 3 — all three of them names that were already
corrupt in the source — and leaves every accented locale name alone. Nothing is
renamed to make room for something else any more, so no `_2` names appear on the
card at all.

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
