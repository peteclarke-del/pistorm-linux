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
install -Dm644 data/icons/hicolor/scalable/apps/pistorm-imager.svg \
        ~/.local/share/icons/hicolor/scalable/apps/pistorm-imager.svg
install -Dm644 pistorm-imager.desktop ~/.local/share/applications/
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor
```

The icon is a scalable SVG in `data/icons`, laid out the way GTK expects a
theme to be, so running from a checkout finds it without installing anything.

## The window

It opens on a choice of three, and nothing else, because a choice with a page of
settings under it is not a choice — the settings are the thing being chosen
between:

| | |
| --- | --- |
| **A basic PiStorm card** | Emu68 and an empty Amiga drive, partitioned and formatted, ready to install Workbench onto from floppies. Leads to which Amiga it is for, what was found to install from, the card and its size, and the plan. |
| **Write a prepared system** | A finished image you have downloaded — CaffeineOS, an Emu68 Hatcher image, or a backup of a card. Leads to the image chooser and the card, and nothing about the machine, because the image brings its own answer to that. |
| **Customise an installation** | The full workflow: sources, storage, the software to add, boot options. Everything the other two decide for you. |

Each screen is laid out in the order its decisions are made, and ends with the
same block: **what this will build**, and `Apply this setup` beneath it. That
block finishes whichever route was taken — the last thing on a quick screen, or
the last thing on the Target page when customising — so the same decision reads
the same way whichever way it was reached.

Nothing is written until that Apply has been pressed. Write stays off before it,
and goes off again whenever something changes what would actually be written —
a partition renamed two pages away puts the setup back to needing another look,
and says so beside the summary. Apply itself is offered only once enough has been
chosen for a card to boot: not merely a configuration that will write, but one
with a Kickstart for Emu68 to map and floppies for an install from floppies. What
is still wanted is named where the button is —

> Still needed: a Kickstart ROM, and 1 more

— and a prepared image is exempt, because the image and a card are the whole
requirement.

`Back` sits bottom left, in the same bar as `Write`, and always returns to the
choice. It withdraws the acceptance with it, so reconsidering the choice that led
to a setup does not leave Write lit while you do.

**Check for updates…** in the menu asks GitHub for this project's releases and
says what it found — the newest with its notes and a way to go and get it, or
that this is already the newest. It is asked for rather than done at startup: a
tool that prepares a card should not reach out to the internet unless someone has
asked it a question. No network, a changed API or a repository with no releases
yet are all reported as the question going unanswered, because none of them mean
anything is wrong with the copy in front of you.

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
    content.py   what a games or demos tree is divided into, and what runs here
    distributions.py  recognising a prepared system and what it expects
    postwrite.py adapting a prepared system after it has been written
    updates.py   asking GitHub whether there is a newer release of this tool
    devices.py   finding and describing removable drives
    prepare.py   partitioning and formatting the target
    util.py      sizes, progress reporting, stream copying
    builder.py   the orchestrator
    jobs.py      job serialisation across the privilege boundary
  ui/            the GTK4 interface
  cli.py         command line front end and privileged writer
  app.py         the GTK application itself
data/icons/      the application icon, in the hicolor theme layout
tests/           unit tests plus a real end-to-end image build
```

## Tests

```
python3 -m unittest discover -s tests -p 'test_*.py' -v   # 326 tests
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
handling described above, optional software copied from a donor system or
fetched from Aminet, prepared systems recognised and adapted after writing, and
per-category exclusions followed through into iGame's list.

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

**Booted in an emulator:** a card built here from PiMiga — its System drive on a
multi-gigabyte PFS3 partition — has been lifted out as an `.hdf` and booted in
FS-UAE, which runs the real PFS3 19.2 handler out of the RDB rather than this
project's own reader. That is what found and then settled five PFS3 writer bugs
that were silent at build time and fatal at mount.

**Not yet tried on hardware:** everything past the basic card. The RTG and
dual-output display handling, including the switcher scripts; optional software
copied from a donor system; multi-partition layouts; adapting a written prepared
system. Those are checked against the format specifications, against independent
tools and in an emulator, which is not the same as an Amiga booting from them.

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

## Prepared systems

Several people distribute a whole, finished AmigaOS installation as an image,
and basing a card on one is far quicker than installing Workbench from six
floppies. Download it from its author, point the **Pre-built image** source at
the file, and the tool names what it found and says what that system expects of
the machine.

**CaffeineOS** is recognised: AmigaOS 3.9 built for Emu68 and the PiStorm, with
Dopus Magellan as its Workbench replacement, its own custom Kickstart on the
boot partition, and its own Emu68 kernel and command line. It wants a 64 GB
card or larger. The detail worth knowing before committing a card to it is that
its Workbench opens on an **RTG screen only** — its own WinUAE configuration
sets `rtg_nocustom=true` — so on a machine watched on the Amiga's own 15 kHz
video there is a desktop nobody can see. The tool says so, and says it more
loudly when the display is set to native.

**PiMiga** is in the catalogue as well, not because it can be used this way but
because it cannot: it is a Raspberry Pi system running the Amiberry emulator,
and its Amiga drives are ordinary folders inside its Linux root partition.
Pointing the image chooser at it explains that, rather than reporting an empty
list of drives.

Recognition is by volume label, which is the one thing that survives an author
repartitioning between releases. An unknown image is never guessed at.

### Adapting one after it has been written

Writing a prepared image copies raw sectors, so none of the file-by-file
compatibility work described below happens to it — which is right, because a
system built for Emu68 already has the drivers it needs. What it cannot know is
which *screen* this machine is watched on. CaffeineOS's startup already branches
on the board it finds and applies `ENVARC:Sys/screenmode.prefs.PI` on a PiStorm;
with no monitor on the Pi's HDMI output, that opens Workbench where nobody can
see it.

An optional pass after writing blanks the saved mode, so the machine keeps the
native screen it started on and a mode can be chosen in Prefs and saved there. It
only ever *removes* a saved choice and never installs one, because which mode
suits a monitor is not something this can know. Blanking a file's data touches no
metadata — the extents are already allocated — which is what makes it safe on a
finished volume, where deleting a file would not be.

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

### What to leave out of a games or demos drive

A WHDLoad collection is arranged by category, and not every category suits every
Amiga: the AGA games on an OCS A500 waste gigabytes on titles that cannot run and
leave iGame offering them. The categories are **discovered from the tree itself**
rather than fixed here, because collections differ and grow — PiMiga's Games
drawer has ten (ARCADIA, BETA, CD32, CDTV, Cinemaware, Foreign, Mags, NTSC, OCS
and AGA) and its Demos drawer four, one of which appears in no other collection
this project has seen.

Each is a switch on the partition, with the count of titles in it. What is fixed
is what a handful of well-known names *mean*, which is enough to propose a
default: AGA and CD32 need AGA, ECS needs ECS, and CDTV does not — it is an A500
with a CD drive, which is easy to assume otherwise. A name nothing is known about
is offered with nothing assumed, so it is never excluded by default. The default
follows the machine and moves with it, and every switch stays changeable, because
"this machine cannot run it" is a sensible default and not a rule.

Leaving a collection out is followed through to **iGame's list**, which keeps an
absolute path to every slave it knows about; entries whose slave will not be on
the card are dropped, honouring the same exclusions the copy uses. On PiMiga's
real list that is 4,201 entries in and 3,886 out. Matching ignores case, because
the list was written on a case-insensitive Amiga volume and is checked against a
Linux tree where `WHDLoad` and `WHDLOAD` are two different directories, and an
entry on a volume nothing here fills is kept rather than dropped unchecked.

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

## Software to add

A Workbench installed from the original floppies is exactly what shipped in
1994: no archiver, no installer, and no idea what WHDLoad is. The pieces most
people add next are offered as a catalogue, grouped as System, Look and feel,
Speed and Networking.

Each one arrives by whichever route it can. Freely distributable software is
**fetched from Aminet and cached** under `~/.cache/pistorm-imager/packages`, so
a second card costs no download. Anything that is not freely distributable —
IBrowse and MiamiDx among them — is only ever **copied out of a donor system
you already have**, which is what pointing at a PiMiga installation is for. A
donor is always preferred over a download.

Whatever can be installed outright is installed, and `Storage/Install` is a last
resort rather than the default: a tick box that produces an installer you have to
find and run has not delivered what it promised. MagicWB's fonts and patterns go
straight into `Fonts:` and `Prefs/Presets`, and its icon set is what gives this
build's own drawers their icons. What still needs running on the Amiga is the
part that *replaces* icons already on the card, because the file system here
creates files and never overwrites them — so VisualPrefs, MCP, NewIcons and
Picasso96, which patch the system or restyle what is already there, are unpacked
into `Storage/Install` and say so in the log. Where a package needs a line to take effect — PeterK's
`icon.library` has to be soft-kicked over the one in ROM, FBlit has to be
started — the build writes `S:User-Startup` to do it.

### What a package needs to actually run

Copying a program's drawer onto the card is not the same as installing it. A
great deal of Amiga software draws itself with **MUI**, and iGame, AmFTP,
WookieChat and NetSurf all do: copied on their own they land on the card, appear
on Workbench, and then do nothing whatsoever when clicked, because
`muimaster.library` is not there. So packages declare what they need, and a
dependency is pulled in whether or not it was ticked — MUI is copied to
`SYS:System/MUI` and given its `MUI:` assign in `S:User-Startup`, which is how a
real MUI install is arranged and how the donor systems carry it.

The same goes for the shared libraries a program draws through, which are kept
apart from the package itself because a program fetched from Aminet still needs
them off the donor: `guigfx.library` and `render.library` for iGame's
screenshots, `codesets.library` and `openurl.library` for the browsers, and the
ReAction classes — `Classes/Gadgets` plus `window.class` and its companions —
without which AWeb opens no window at all. A library wanted by three packages is
copied once; the file system here creates files and refuses to overwrite them, so
a second copy would not merely be wasteful, it would end the build.

### Why half the icons were blank

A modern Amiga icon keeps its picture in an **OS3.5 colour chunk appended after
the classic one**, and often leaves the classic planar image at 0x0 - iGame's
does exactly that. Kickstart 3.1's `icon.library` 40.1 knows nothing about that
chunk, so it draws nothing at all, and a card full of perfectly good icons comes
up with half of them blank. PeterK's `icon.library` reads them, which is why the
systems these files come from show them.

Copying the replacement into `LIBS:` is not enough, and neither is soft-kicking
it from `S:User-Startup`: by the time that file runs, `IPrefs` has already
opened the one in ROM, and a library in the system list cannot be replaced.
Booted and asked directly, the Amiga answered `icon.library 40.1` while 51.4 sat
unused in `LIBS:`.

So it is installed with **`LoadModule`, inserted above `IPrefs` at the top of
`S:Startup-Sequence`** - which is what the donor systems themselves do on the
third line of their own startup. The file cannot be rewritten afterwards,
because this file system creates files and never overwrites them, so it is
edited in flight on its way off the floppy image. Asked again after that change,
the Amiga answers `icon.library 51.4`.

### Updates and patches

Installing from the original floppies gives you exactly what shipped in 1994,
and some of that is not fit for the machine it is going onto. A PiStorm is a
**68040-class accelerator**, and Workbench 3.1's idea of a 68040 is
`SetPatch 40.16` from February 1994 and `68040.library 37.30` — both older than
the CPU they are meant to set up.

So a build from ADFs offers, and by default takes, two updates:

| | |
| --- | --- |
| **68k CPU libraries (MMULib)** | Thomas Richter's maintained replacements, fetched from Aminet: `68020` through `68060`, `680x0`, `mmu`, `memory` and `softieee`. `68040.library` goes from 37.30 (1994) to **47.1 (2022)**, `mmu.library` to **47.11 (2025)**. |
| **A SetPatch that knows about the 68040** | 44.38 in place of 40.16. Commodore's own, from a later release, so it can only come from a system you already have — it is not on Aminet. |

The floppies' `C:SetPatch` is **refused during the install** rather than
overwritten afterwards, because this file system creates files and never
replaces them; the newer one is then copied into its place.

### What a program needs is read out of it

Declaring dependencies by hand caught MUI and a handful of libraries and missed
twenty more, each of which copied onto the card perfectly and then would not
run: `bsdsocket.library` for the network clients, `ixemul` and `netinfo.device`
for NetSurf, `Picasso96API` for AWeb, `screennotify` for Birdie, `popupmenu` and
`vapor_toolkit` for the MUI applications.

Amiga binaries name what they open as plain strings, so the answer is in the
files themselves. Everything a copied program mentions, that will not be on the
card and that the donor system has, is copied too - and then the same question
is asked of *those*, because a library brings its own needs with it
(`mmu.library` wants `68030.library`, `ixemul` wants `ixnet`, `xpkmaster` wants
`xfdmaster`). One round left seven behind; repeating until a round finds nothing
new brought the real card down from nineteen missing files to three, and those
three are optional (`narrator.device` is speech synthesis).

The scan over-matches where two strings abut in a binary, which costs nothing: a
name that is really a fragment of another resolves to nothing in the donor and
is dropped.

**Key files travel with what they unlock.** Registered Amiga software looks for
`<name>.key` beside the system rather than in its own drawer, so copying
`xadmaster.library` without `S:xadmaster.key` leaves it crippled in a way that
reads as the copy having failed. Anything being copied that has a matching key
in `S:`, `L:` or `DEVS:keyfiles` takes it along — which picks up MUI's key too.

**Some things no scan can find.** A WHDLoad slave asks for the Kickstart the
game expects, and those are ROM images, not code: nothing names them inside a
binary. Without `DEVS:Kickstarts` iGame launches a game and the machine falls
over on the spot, so WHDLoad asks for that drawer outright. It is 6.8 MB. Settings are carried too, since files alone are not a working
install - `ENVARC:mui`, `ENVARC:AWeb3` and `ENVARC:ClassAct`, and the
`AWEB_APL:` assign that AWeb is found through.

### Drawers you can actually see

A drawer with no `.info` beside it does not appear on Workbench — it can only be
reached from a Shell or by turning on **Window/Show/All Files**. That is correct
for `C:` and `LIBS:`, which is why Commodore ships them without icons, but this
tool also creates drawers of its own — `Programs`, `Internet`, `AmiTCP` — and
gave them none either, so every browser and launcher that was installed could
not be found from the desktop. It looked exactly like the software never having
been installed. `Storage` had the same problem: the real Commodore installer
creates that drawer *and* its icon, and installing from the ADFs creates only the
drawer.

Every drawer this build makes now gets an icon, taken from a real Amiga icon
rather than invented — the chosen icon set, or the donor system — matched on the
drawer's own name and otherwise any drawer icon among them. Two things decide
which one is usable:

* **It must be a drawer icon.** Icons are typed, and only a drawer icon opens a
  drawer; a project icon tells Workbench to run its default tool, so a drawer
  wearing one answers *unable to open script* on a double click. Matching purely
  on the name gave the `Storage/Install` drawer MagicWB's `Install.info`, which
  is the project icon for MagicWB's own installer script.
* **Its remembered position is cleared.** An icon copied from elsewhere brings
  that drawer's snapshotted coordinates with it, so several drawers given the
  same fallback icon all claim one square of the window and land on top of each
  other.

**Suggested load** picks a set from the machine and the display, because the
right answer genuinely differs:

| | OCS/ECS on the Amiga's own video | AGA, or Workbench on the Pi's HDMI |
|---|---|---|
| Drawing | FBlit and FText move Workbench's drawing off the blitter and into fast RAM, which is where a PiStorm's speed is | no blitter in the way; Picasso96 is the point of it |
| Palette | FullPalette locks the desktop colours so a program cannot scramble them | a deep display has colours to spare |
| Icons | MagicWB's eight colours suit a limited palette | a heavier desktop such as Scalos becomes affordable |

Common to both: WHDLoad, LhA, Installer, a faster `icon.library`, MagicMenu and
VisualPrefs. Networking — the PiStorm's `vlink.device`, a TCP/IP stack, AmiSSL
and NetSurf — is suggested when a WiFi network has been configured.

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
  commented out, so they cannot fail the boot;
* `S:WHDLoad.prefs` is cleaned the same way. This is where WHDLoad's settings
  actually live — the quit key, whether it forces PAL, and the hooks it runs
  around every game — and PiMiga's copy sets `ExecuteStartup` and
  `ExecuteCleanup` to `uae-configuration`, Amiberry's own control program. Carried
  over unedited, a card runs a missing command before and after every single game.

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
