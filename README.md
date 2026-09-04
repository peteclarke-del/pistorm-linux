# PiStorm Imager for Linux

A GTK4 desktop application for preparing PiStorm / Emu68 SD cards on Linux.

The official [Emu68 Imager](https://mja65.github.io/Emu68-Imager/) is Windows and
PowerShell only. This is a native Linux replacement, and it additionally
understands pre-built images such as **PiMiga**, so you can write one to a card
and still apply your own Emu68 build, Kickstart, video mode and WiFi settings on
top of it.

## What it does

Four tasks, all ending with the same boot-partition customisation pass. Two of
them take a `.hdf`, and the difference between them is the whole point: **Write
a drive image unchanged** keeps the image's own partitions and file systems and
adds nothing, while **Build a new card** can take the *files* out of that same
image, put them on a layout of your choosing, and add the Workbench disks and
software to them. That is why a ClassicWB card is built with the second: its
drive brings no Workbench, so it needs the floppies alongside it.

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
python3 -m unittest discover -s tests -p 'test_*.py' -v   # 414 tests
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
handling described above, optional software fetched from its publisher,
prepared systems recognised and adapted after writing, and
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
fetched and installed; multi-partition layouts; adapting a written prepared
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
switch owns `move_slow_to_chip` and anything else in the box was typed by hand.
Both survive, and turning the switch off removes only its own option. The switch
is asked when the configuration is gathered rather than only when a quick setup
is applied - the two were separate records of one fact, so a setup loaded with
the switch on and the option missing built a card without it: 512K of chip RAM
on a machine that had been told to give it a megabyte, with the switch on screen
still saying it was on.

### Every option the machine decides has to reach the card

The card is written from `gather()`, and `gather()` built its boot options
from the widgets alone. Two settings have no widget — the machine or the
display decides them — so both sat at their dataclass default on every card
written from the pages:

| Option | Decided by | What its absence did |
| --- | --- | --- |
| `enable_slow_ram` | an OCS/ECS machine | `move_slow_to_chip` had nothing to move: 512K of chip RAM on a machine told to give Workbench a megabyte |
| `unicam`, `unicam_smooth` | the Framethrower display | choosing that display wrote no overlay to drive it |

A save/load round trip cannot catch this: a field never set at all is
consistently wrong in both directions, so it survives the comparison. The
guard is an invariant instead —
`EveryOptionTheMachineDecidesReachesTheCard` asserts that everything
`machines.boot_options()` can decide is either passed by `gather()` or owned by
a widget it reads. It was proved by putting the bug back and watching it fail.

### The trapdoor RAM has to be mapped before it can be moved

`move_slow_to_chip` moves the trapdoor RAM at `0xC00000` into the chip range.
It can only move RAM that has been mapped, and mapping it is a different set of
options — `enable_c0_slow`, `enable_c8_slow`, `enable_d0_slow` — which Emu68
takes for any OCS or ECS machine. Sent on its own, `move_slow_to_chip` is inert.

Nothing on screen decides those: the *machine* does. `machines.boot_options()`
set them, and that runs only where a quick setup is assembled — while the card
is written from `gather()`, which built its boot options from the widgets alone
and so left the field at its default. **Every card this tool wrote went out
without them**, and the symptom was the same 512K of chip RAM the paragraph
above describes, now with the option that was supposed to fix it present and
doing nothing. It was found by reading `cmdline.txt` back off a written card:

    vc4.mem=64 chip_slowdown dbf_slowdown blitwait move_slow_to_chip

The option names were then checked against the strings inside the Emu68 kernel
binary rather than taken from memory, because a switch spelled wrongly does
nothing at all and says nothing about it. `gather()` now asks
`machines.wants_slow_ram()` for the same answer the quick path gets, so there is
one rule rather than two, and the same card reads:

    vc4.mem=64 chip_slowdown dbf_slowdown blitwait enable_c0_slow enable_c8_slow enable_d0_slow move_slow_to_chip

## Testing a card in an emulator

The Amiga to emulate is the one the card was built for, and that description
already exists: `pistorm_imager/core/emulate.py` turns a `Machine` into an
FS-UAE configuration so it is never written twice.

That matters because the hand-written harness used through one long bisection
had drifted into describing a different machine entirely — `amiga_model =
A1200` (AGA, not the ECS A500 in question), `fpu = 68040` on an accelerator
that has no FPU at all, and `accuracy = 0`, which runs a fast, inexact 68040 on
which WHDLoad cannot start a single game. That last one cost hours of hunting a
defect in the imager that was a flag in the emulator. The module fixes
`accuracy = 1`, takes the model from the chipset, the chip RAM from the
trapdoor choice, and asks for no FPU.

One caveat is recorded rather than papered over: FS-UAE 3.0.3 accepts
`fpu = none` silently and says nothing either way, so whether it takes effect
is **unverified**. Assume floating point code may still run in the emulator and
guru on the real machine.

Attach **the whole `0x76` partition**, not the bootable drive alone, so that
every drive mounts and can be checked — and copy it *exactly*. A copy one
mebibyte short of the partition made the last drive come up as `NDOS`, because
PFS3 keeps a copy of its root block at the end; that looked exactly like a
formatting bug in this tool and was not.

## How big is the card, and which gigabyte do you mean

A size typed for a card is a guess at what the card holds, and the two meanings
of "GB" make it a bad guess. `125G` is 125 GiB - **9.22 GB more** than a card
sold as 125 GB - so an image built from it does not fit the card it was built
for. The parser has always distinguished the two (`125GB` decimal, `125GiB` and
a bare `125G` binary, a bare number MiB), but that only helps someone who knows
to ask.

So the size is not typed at all when it can be known instead. **Writing to a
card, its capacity is read from the card**, the box is closed, and the title
says which card and both readings of its size:

> Card size - taken from mmcblk0, which holds 116.42 GiB (125.00 GB as cards
> are sold)

The configuration takes it from the device rather than from any box, so a
number left in one from an earlier session cannot reach a build.

**Writing an image file** there is no card to ask, so the box stays open and
the size line says which reading it took: `125G` is answered with *"is binary;
write 125GB for a card sold as that size"*. An explicit `GB` or `GiB` is left
alone, having said what it meant.

Whichever way, the drives have to fit what was asked for. Nothing checked, so a
16 GiB image asked to hold 40 GiB of partitions was accepted and laid out past
its own end; now it is refused, counting the boot partition and the alignment
before it - except on a bare `.hdf`, which has neither.

### What a saved setup carries, and what it must not

A setup is saved as a configuration plus the interface state, and the second is
for **what a BuildConfig cannot express**: the machine, the display, the folders
that were browsed to. Anything the configuration already carries must not be
written there as well. The target and the card size were, taken from the quick
screen's own copy, which goes stale the moment either is set on its own page -
and the interface state is applied *after* the configuration, so the stale copy
won. A setup naming a 125 GiB image came back as a 59 GiB SD card.

The same omission in the other direction lost the rest: the partitions, the
software and the donor it came from, the Emu68 release and the board were all
saved faithfully and never put back. Each was found only when somebody noticed
it missing, so the GUI test now saves a setup, scrambles the widgets, loads it
again and compares **every field** of the configuration. That check found the
last two by itself: the board, which the machine reset after the configuration
had restored it, and the card size, which was written back into the box as
"37.25G" and read out again a little smaller each time.

Putting a loaded setup back is therefore one method rather than a sequence
repeated at each call site, because the order is the whole of it: the machine
and the display arrive with the interface state, and they decide which software
suits the card and which board the Source page shows, so both of those are
restored from the configuration afterwards.

Two things are only true a moment later, and the summary has to be told when
they become true. The Workbench disks are identified in a background thread,
and the list of Emu68 builds arrives from GitHub after the window is already
up; the summary is written before either, so it says an Emu68 release is still
needed. The scan rewrote the summary when it finished and the release list did
not, which is why a setup loaded at startup went on saying *"Still needed: an
Emu68 release"* with the release chosen and everything else in place. Both
paths refresh it now, whether the list arrives or the fetch fails.

**Forget the saved setup** puts the window back as it opened. It only deleted
the file, so nothing on screen changed and only the *next* launch differed,
which is not what starting again means. Clearing the widgets by hand was not it
either: the storage layout stayed exactly as it was, because the relayout gives
up when there is no target to lay anything out for, so the drives someone had
arranged survived a reset that claimed to have removed them. The reset now goes
through `apply()` - the same method a loaded setup goes through - with a default
configuration, so it reaches every widget the configuration reaches without a
list to keep in step, and finishes back on the opening choice.

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

**It applies to any system built elsewhere, not only a whole image.** A drive
imported into DH0 from an `.hdf` on a card this build partitions was set up on
somebody else's machine and watched on somebody else's screen in exactly the
same way, and the pass ran only for images written as they were. The switch was
part of the image chooser, on a page such a build never shows, so there was no
way to ask for it either. It lives with the display on the **Amiga** page now,
appears whenever a ready-made system is involved, and one predicate —
`BuildConfig.brings_a_system_from_elsewhere()` — decides both.

### A rev 6A A500 is not necessarily OCS

Fitting a Super Denise to a rev 6A board makes it a full **ECS** machine, and
that is a common enough upgrade that offering only a plain OCS A500 gets the
chipset wrong for a real machine. The chipset decides which game collections
are worth copying and which screen modes exist, so there is a separate
**Amiga 500 with ECS** to choose, and the plain A500's note points at it.

## Refused, warned about, or allowed

Three different things, and the tool now keeps them apart.

**Refused** - it cannot work, so nothing is written. A card whose system drive
brings no Workbench and no floppies to fill it in stops at a Shell saying
`C:Version: Unknown command`; the drives adding up to more than the card holds;
a bootable drive told to be filled two ways at once.

**Warned about** - it will build, and probably is not what was meant. These are
said in the summary where the setup is accepted, and again in the log before
anything is written, and then the build goes ahead:

* games or demos on the card with no WHDLoad to launch them
* iGame installed with no drive being filled with games, so it opens empty
* an RTG display chosen with no Picasso96 and no imported system that might
  carry one
* Workbench set to open on an RTG screen the card has not got
* nothing at all going onto the Amiga drives
* software chosen whose archive nobody can fetch on your behalf

**Allowed silently** - everything else.

## A boot script is not an operating system

Such a drive **must** be given the disks: a card made from it alone stops at a
Shell saying `C:Version: Unknown command`, so building that combination is
refused rather than written. The drive is written first and the floppies add
only what it has not got - nothing its author put there is
replaced - so a ClassicWB card can be built with `C:LoadWB` and the rest in
place. Its own installer copies the same files with `copy DF0:C/... SYS:C CLONE` and
then puts the boot script it carries as `T:Science` in place of its own; doing
that here saves feeding it floppies on the Amiga. It is only done when the disks
are being installed too - taking an installer away without doing its work leaves
a card that cannot boot at all, which is worse than one that asks for a disk.

And software can be added to an imported drive at all. The list was shown only
for a Workbench installed from floppies - "only a Workbench built from floppies
needs anything added to it" - while the build applies package overlays to an
imported drive exactly the same way. A card built around somebody's drive could
not be given WHDLoad or iGame.


An imported drive was called a complete system if it had `S:Startup-Sequence`.
ClassicWB has one, and it is an **installer**: on the first boot it says

> You'll need a valid Workbench 3.0/3.1 disk, without one the install will
> fail. Vital and copyright files contained on the disk will be copied during
> installation. This is required because Workbench is still sold commercially.

Its drive carries no `C:LoadWB`, no `C:IPrefs`, no `workbench.library` and no
`diskfont.library`, because those belong to Commodore and cannot be given away.
Reading it as finished offered a card that boots straight into an installer
asking for a floppy drive.

So a drive needs the Workbench disks unless it has both a boot script **and**
`C:LoadWB`, and the description says which of the two is missing. Needing them is
also now *asked* for: the demand was made only when a folder had already been
chosen - `install_amigaos` is false until then - so a card that needs the disks
and has none said nothing at all, and built. What decides it is what the setup
needs, which is known before any folder is, and the chooser is moved beside the
drive that needs it rather than left on a page the quick start never shows.

A drive is judged to need the disks only when it was actually read and found to
lack them. An image this reader cannot open says nothing either way, and
treating that as "needs the disks" would demand floppies for a perfectly good
drive on the strength of not having understood it. The images searched are the
one chosen on the quick screen *and* whatever fills the bootable drive on the
Storage page, because those are two routes to the same card; the answer is
cached against the file's modification time, since the summary asks on every
redraw and the question costs an image read. The plan says so
too: an imported drive with the disks installed alongside it reads *"the files
out of an Amiga hard disk image, with Workbench from your floppy images filling
in what it does not carry"*, where it named only the image before - the summary
of the very setup that produced an unbootable card looked as though the disks
had been ignored.

The plan for the two tasks that take the whole Amiga side from a file -
building a card around an `.hdf`, and writing a prepared image unchanged -
now describes **the drives inside that file**, read out of its own RDB (or
the single bare file system, for an image with none). It used to walk the
configuration's partition list, which those tasks never use, and so announced
an empty DH0 - *"left empty - format it on the Amiga"* - on a card whose whole
point was the drive in the image.

The
distribution's own `Real_Amiga_Install.ADF` is a separate thing again: a floppy
that unzips a `System.zip` onto a formatted DH0 and repairs the protection bits
`unzip` destroys. Importing the drive directly needs none of that - the files
are read out of a real Amiga file system with their protection bits intact.

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

Every one of them comes **from whoever publishes it** — Aminet, or the project
that makes it — and is cached under `~/.cache/pistorm-imager/packages`, so a
second card costs no download.

Software used to be able to come out of a *donor system* instead: a Workbench
drive or a PiMiga folder the user pointed at, which the build mined for
whatever it held. That is gone, along with the "Take it from" chooser. It meant
a card was built from whatever some other installation happened to contain, at
whatever age, and nothing said which. Everything in the catalogue now names its
own source, and where a package once had only a donor it either found a real
one or left the list:

| Was donor-only | Now |
| --- | --- |
| ClickToFront | `util/mouse/ClickToFront.lha` |
| Directory Opus 4 | `util/dopus/DirectoryOpus-4.18.22.lha`, the GPL 4.18 release |
| HippoPlayer | `mus/play/hippoplayer.lha` |
| Scalos | `util/wb/Scalos.lha` |
| AmFTP | `comm/tcp/AmFTP191.lha` |
| WookieChat | `comm/irc/WookieChat2.11_OS3.lha` |
| MiamiDx (`network`) | **Replaced.** The device it needed was the donor's `vlink.device`, which nobody publishes. Emu68's own release carries `wifipi.device` for the wireless chip the Pi actually has, so that is the network card now, with the firmware for every Pi model, and Roadshow's interface file names it. |
| IBrowse | **Dropped.** Commercial, and not distributable. NetSurf is the browser. |
| AWeb | **Dropped.** Aminet's `AWeb.lha` is a 3.2 demo; the free APL release is a per-CPU build whose 68020 binary carries floating point instructions, and [a PiStorm has no FPU](#a-pistorm-has-no-fpu). |
| A newer SetPatch | **Dropped.** Commodore's, from a later release, undistributable — and it stopped every WHDLoad game from starting. |
| Backdrops and boot pictures | **Dropped.** They were another distribution's artwork. |

One thing genuinely goes with the donor: **WHDLoad's `DEVS:Kickstarts`**. Those
are Commodore ROM images, nobody publishes them, and most slaves will not start
without the one the game expects. The package says so where it is chosen rather
than letting a game launch and take the machine down.

Whatever can be installed outright is installed, and `Storage/Install` is a last
resort rather than the default: a tick box that produces an installer you have to
find and run has not delivered what it promised. What still needs running on the
Amiga is the part that *replaces* files already on the card, because the file
system here creates files and never overwrites them — so VisualPrefs, MCP,
NewIcons, Scalos and Picasso96, which patch the system or restyle what is
already there, are unpacked into `Storage/Install` and say so in the log. Where a package needs a line to take effect — PeterK's
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

### Updates and patches, and why they are off

Installing from the original floppies gives you exactly what shipped in 1994.
A PiStorm is a **68040-class accelerator**, and Workbench 3.1's idea of a 68040
is `SetPatch 40.16` from February 1994 and `68040.library 37.30` — both older
than the CPU they are meant to set up. Replacing them looks like an obvious
improvement.

**It stops every WHDLoad game from running.** Either one is enough on its own:
`SetPatch 44.38` leaves a game hanging on a black screen, and MMULib's
libraries give a yellow screen — a CPU exception, with no operating system left
to draw a Guru. This was established by building the same card four times,
changing one thing at a time, against a card proven to run the game.

So they are offered and **not taken by default**, and neither is required by
anything. They are worth having on a machine used for applications, where the
newer CPU support is the point and no game is going to take the hardware over.
On a card built around a WHDLoad collection, leave them alone.

Two updates are offered:

| | |
| --- | --- |
| **68k CPU libraries (MMULib)** | Thomas Richter's maintained replacements, fetched from Aminet: `68020` through `68060`, `680x0`, `mmu`, `memory` and `softieee`. `68040.library` goes from 37.30 (1994) to **47.1 (2022)**, `mmu.library` to **47.11 (2025)**. |
| **A SetPatch that knows about the 68040** | 44.38 in place of 40.16. Commodore's own, from a later release, so it can only come from a system you already have — it is not on Aminet. |

### Which copy wins when a drive already has one

The file system here creates files and never overwrites them, so when two
sources offer the same file **the one that lands first wins**. That was being
settled by the order the build happened to run in, and it produced three
separate faults:

1. **The software never reached an imported drive at all.** Packages were
   applied only by the floppy-install pass. Import a drive as DH0 without
   ticking the Workbench disks and every program in the list was silently
   left off the card.
2. **With both, the floppy install was thrown away.** `_install_amigaos`
   formatted DH0, installed Workbench and applied the packages; the content
   pass then re-created the same drive from the image, destroying all of it.
   A card built that way is the imported distribution and nothing else — which
   is exactly what a card built here turned out to be when its `Programs`
   drawer was read back: fifteen programs, none of them from this catalogue.
3. **A package could never replace an older copy.** Whatever the drive or the
   floppies carried was there first, so the current release the user had
   ticked was skipped as "already present".

All three are fixed. The floppy install is skipped when the boot drive is
filled from an image — the content pass fills it and takes what the disks
provide for the gaps — and the packages, the drawer icons and `S:User-Startup`
are applied there instead. Packages are resolved **before** the drive is
filled, so they can take the place of an older copy.

**Displacing stops when the filling does.** Refusing a path is a rule about
copying a drive, and the package's own files go on through the same pass — so
leaving it switched on refused those too, and the file landed nowhere at all.
Whole drawers were unaffected, which is what made the resulting card look like
a packaging problem rather than this: `Utilities/PowerWindows`, `Internet/
NetSurf` and `Programs/iGame` were all present and correct while `C:WHDLoad`,
`C:LhA`, `Libs:icon.library` and `Programs/iGame/iGame` were simply absent.

A drawer claims its **name** as well. ClassicWB keeps `Visage` as a *file* in
`Utilities:` and this build wants a drawer of that name there — a collision
that ended an hour-long build outright with *"Visage already exists as a
file"*. The name is freed the same way, and safely: the copy asks about files
and never about drawers, so claiming a name can only ever displace a file, and
a drawer of the same name is merged into as before. Its contents are never
touched one by one.

And no single package may destroy a card again: an overlay that cannot be
installed is reported as a warning and the build carries on with the rest.

Whether they do is **asked**, not assumed. *"Replace older copies already on
the imported drive"* sits with the software list and appears only when a drive
is actually being imported. On, the release you ticked is installed in place of
the drive's; off, the drive's own copy is kept. Only whole files are ever
displaced — a drawer is merged into what is there, and refusing one during the
copy would take the drive's own contents with it — and only paths a package has
already fetched, so a failed download can never leave the card without the file
it refused.

### Where the software actually comes from

Three sources were wrong or second-best, and reading a built card is what
showed it:

- **WHDLoad** came from `dev/misc/WHDLoad_usr.lha`, which is a 2007 upload of
  16.8 that has not moved since. **Changing the source was not enough**: the
  cache is keyed on the file name and both publishers serve
  `WHDLoad_usr.lha`, so cards went on being built from the archive already
  downloaded while the catalogue said 20.0 — caught by reading the version
  string off a finished card, not from the build log, which reported the
  cache hit perfectly honestly. A cached archive now records the address it
  came from, and one of unrecorded or different origin is fetched again. The card being built against it came out
  *older* than the ready-made distribution it was competing with (18.2). It now
  comes from the author's own site, which serves 20.0.
- **LhA** was left in `Storage/Install` as a self-extracting Amiga program to
  run by hand, so a card could arrive with no archiver at all. An archiver has
  to be shipped that way — you need one to unpack the other — but the archive
  inside is an ordinary LhA one, so it is taken out here and the 68040 build
  installed as `C:LhA`.
- **Birdie** and **PowerWindows** were staged with a note asking the user to
  copy them into place. Birdie now goes into `C:` with its patterns, and is
  started from `S:User-Startup` the way its own documentation says; PowerWindows
  goes into `Utilities/PowerWindows` whole, because it looks for its external
  routines beside itself.

One bug fell out of that work: `fetch()` chose "place the archive whole" on
whether a package listed `items`, so a package that placed its files by
`rename` instead took that branch and its entire archive went to `stage` —
which for such a package is `""`, the volume root.

### Leaving out what this machine cannot run

A collection keeps its titles in a container drawer — `WHDLOAD` is the usual
one — divided into categories whose names say what they need, and those have
always been offered as things to leave out, with the ones this machine cannot
run switched off to start with.

**Everything beside that drawer was offered nowhere.** A Games drive with forty
native titles sitting next to its `WHDLOAD` collection could only be taken
whole, so `Turrican2AGA` went onto an ECS machine along with the rest. Those are
listed now too — one entry per program, on any drive being filled from a folder,
games and demos alike — so anything can be suppressed whether or not this tool
can judge it.

What it *can* judge, it judges from the title's own name: `AGA` or `CD32` in
UPPER CASE at a word boundary means AGA, so `Turrican2AGA` and `DeepCoreCD32`
start switched off on an OCS or ECS machine while `Saga`, `Vagabond` and
`AgaMemnon` are untouched. Everything else is listed with no requirement and
left in, because the honest answer is that we do not know.

**A folder and an image are asked the same question.** The listing used to walk
a host directory, so a drive imported from an `.hdf` was offered nothing to
leave out and could only be taken whole. The FFS and PFS3 readers both list a
directory by name, so the same walk works on either — one directory at a time
rather than over the whole drive, which on twenty gigabytes of games would take
longer than the build.

A loose *file* is listed only when its own name says what it needs. That is not
fussiness: `Turrican2AGA` on a real drive is a fourteen-byte launcher rather
than a drawer, so a rule about drawers alone missed the one title on the whole
drive that could be identified — while listing every file would have buried it
among save files and icons.

**Leaving out a launcher takes what it runs with it.** `Turrican2AGA` is
fourteen bytes reading `AmigaGame.exe`, so removing the title and keeping the
170 KB program it names wastes the very space the exclusion was for, on
something nothing can now reach. The reference is followed one step, and two
things stop that doing harm: a launcher that **stays** pins what it names, so a
shared engine survives as long as anything still runs it; and anything offered
as a choice of its own is never taken away behind the user's back. Each one
followed is named in the log.

**Reading the binaries was tried and abandoned.** FMODE and BPLCON4 are
AGA-only registers, so scanning a program for them looks like a real test. It
is not: matching 16-bit words finds "FMODE" 56 times inside `DOOM1.WAD` and 48
times inside an IFF picture. It labels data as code, and would have confidently
condemned titles that run perfectly well. A name is a weaker signal, but it is
never a guess.

### The drive's own S:User-Startup is kept, and added to

A package that has to be *started* — FBlit, FText, Birdie, BlazeWCP — puts its
line in `S:User-Startup`. A drive being imported brings its own, and this file
system creates files and never overwrites them, so the build said

    S:User-Startup already exists; left alone

and those four went onto the card as programs that were never run. Read off a
finished card, every one of their lines was absent; the only reason MUI's
appeared was that ClassicWB's own file happens to carry identical assigns.

The drive's file is now held back during the copy — the same trick that lets a
distribution's real boot script replace its installer — and written out again
whole with the packages' lines appended after it. Left whole deliberately: it
is the distribution's own setup, and replacing it would break the system the
card is built on.

While there, the display-switching scripts were made tolerant of a drive that
already carries them. They were written with `check_existing=True`, so a card
built by this tool once before would end the *next* build at its last step,
an hour in, over a script that was already correct.

### The card says what was put on it

AmigaOS has no uninstaller. Commodore's Installer only ever installed — it has
no removal facility — and the third-party tools on Aminet that fill the gap
either read Installer's log file (`util/wb/Uninstaller.lha`) or watch an
installation as it happens and record what changed (`util/misc/DeInstaller.lha`).
Neither helps here, because nothing this tool installs goes through Installer
at all: the files are copied into place directly, which is the whole point of
installing rather than staging. So there is no log to undo.

Taking a package back off a finished card therefore meant reading an hour-old
build log, if it was still on the screen. Every build now writes its own record
to `S:PiStorm-Installed` on the drive the machine boots from — readable on the
Amiga with `Type`, and grouped by the package that asked for each path:

    ; WHDLoad
    C/WHDLoad
    C/WHDLoadCD32

    ; SysInfo
    Utilities/SysInfo  ; whole drawer, 34 files

The distinction in that example is the one that matters. A package bringing its
own drawer is named as the drawer, because deleting it removes exactly that
package and nothing else. A package that merges into a drawer the *system*
owns — WHDLoad puts three commands into `C` — is listed file by file, because
naming the drawer there would read as an instruction to delete `SYS:C` and take
AmigaDOS with it.

The lines added to `S:User-Startup` are recorded too, commented out. A line left
behind runs a program that is no longer there, which is a boot-time error every
time the machine starts.

A card built from a drive this tool produced earlier brings that earlier
build's record with it, describing software that is not there and missing
software that is. It is held back during the copy, the same way the drive's
`S:User-Startup` is, so this build's record lands in its place.

### Anything kept from a previous run has to say where it came from

Three rebuilds were lost to one shape of bug, in three different places: a
thing kept from an earlier run and handed back although what it came from had
changed. Each one built a card that looked right and was not, and each was only
found by reading the finished card rather than the build log — which reported
the cache hit perfectly honestly every time.

| Cache | Was keyed on | Now |
| --- | --- | --- |
| The downloaded archive | its file name — and Aminet and whdload.de both serve `WHDLoad_usr.lha` | the address it came from, recorded beside it |
| The unpacked tree | the archive's name | discarded when the archive is newer than it |
| Emu68's RTG driver | existence alone | the release URL it was extracted from |
| The Raspberry Pi firmware | existence alone, with no check of what arrived | the source URL, and `Content-Length` |

The unpacked tree is the one that stung most: every package was correctly
re-downloaded and then installed from the tree unpacked hours earlier, so a
card came out carrying WHDLoad 16.8 while the 20.0 archive sat beside it in the
same directory.

`EveryCacheKnowsWhereItCameFrom` in `tests/test_updates.py` walks the code and
asserts each of these still checks its provenance, so the next cache added has
to as well.

### iGame is told where the games are

iGame keeps the drawers it scans in `repos.prefs`, and its Aminet archive
ships none. Installed cleanly it came up with **nothing to scan**: "Scan
Repositories" found nothing and the list stayed empty on a card whose drives
were full of games. Found by booting a written card in an emulator and
watching iGame open its repositories requester with nothing in it.

The build knows exactly which drives it filled, so it says so — one line per
drive it put content on, naming the `WHDLoad` drawer inside only when that
drawer is really there. Nothing is guessed: a drive this build did not fill is
not named, because pointing iGame at a drawer that does not exist is precisely
what the donor's own list used to do.

### What a program needs comes with it

Dependencies between packages are declared and pulled in: iGame, AmFTP, NetSurf
and WookieChat are MUI applications, and copied on their own they land on the
card, appear on Workbench and then do nothing at all when clicked.

The shared libraries a program draws with come from the same archive that
brings the program. There used to be a scanner for this: Amiga binaries name
what they open as plain strings, so everything a copied program mentioned that
the *donor* had was copied too, transitively. It found nineteen missing files
where hand-written declarations had found three. It also only ever worked
because there was a donor system to mine, and with everything coming from its
publisher there is nothing to scan against — an archive that needs
`codesets.library` ships it.

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
rather than invented — the chosen icon set, or failing that the Workbench
disks — matched on the drawer's own name and otherwise any drawer icon among
them. Two things decide
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

### Making a stock Workbench pleasant

Workbench 3.1 out of the box is sparse in ways that are easy to forget until
you use it. Two of these are on by default because their absence is the first
thing anyone notices:

* **DefIcons** gives every file an icon chosen from what it actually is.
  Without it a window shows programs and nothing else, which is most of why a
  stock desktop looks so bare.
* **FreeWheel** makes the mouse wheel scroll the window under the pointer.

Beyond those, offered rather than assumed: **ClickToFront**, the **backdrops
and boot pictures** from the system you are copying from (several megabytes of
them, so worth a thought on a small system partition), a **Dock-It** dock along
the screen edge, **Visage** for pictures, **SnoopDos** for when something will
not start and you need to see what it is looking for, and **Directory Opus 4**
as a real file manager.

For music there is **AMPlifier** (modules, MP3, skins) and **DigiBooster 1.7**
as an eight channel tracker, and **HippoPlayer**, the classic lightweight
player - all three from Aminet.

**Suggested load** picks a set from the machine and the display, because the
right answer genuinely differs:

| | OCS/ECS on the Amiga's own video | AGA, or Workbench on the Pi's HDMI |
|---|---|---|
| Drawing | FBlit and FText move Workbench's drawing off the blitter and into fast RAM, which is where a PiStorm's speed is | no blitter in the way; Picasso96 is the point of it |
| Palette | FullPalette locks the desktop colours so a program cannot scramble them | a deep display has colours to spare |
| Desktop | the stock icons, drawn for exactly this palette | a heavier desktop such as Scalos becomes affordable |

Common to both: WHDLoad, LhA, Installer, a faster `icon.library`, MagicMenu and
VisualPrefs. Networking — the Pi's WiFi as an Amiga network card, Roadshow,
AmiSSL and NetSurf — is suggested when a WiFi network has been configured.

**An RTG display brings Picasso96 with it, and holds it on.** Picasso96 *is*
the RTG subsystem; Emu68's driver is a card for it, and without it a card set
up for the Pi's HDMI output has no RTG screen modes to open on. It was an
ordinary tick box beside the display choice, and nothing rebuilt the software
list when the display changed — so choosing both outputs left it off, silently.
Choosing a display that draws on the Pi now ticks it and locks it, and says
why in the row.

## Every package names its source

A published release is the newest there is; a donor's copy was whatever its
author installed, which may be years old, and there was no way to tell from the
card which had happened. So there is one route now — the publisher's — and a
package that cannot be fetched says so before the build rather than in the log
afterwards:

> WARNING: Roadshow could not be fetched from http://roadshow.apc-tcp.de/, so
> it is not on this card

One archive genuinely cannot be downloaded: APC&TCP serve Roadshow only to a
browser. That is marked `manual`, the setup summary says so while there is
still time to do something about it, and the build uses a copy put in
`~/.cache/pistorm-imager/packages` by hand rather than caching a login page as
though it were the archive. A
download that **stops early is no longer kept** - it is still a file, and
caching a truncated archive means every build afterwards fails to unpack
something that looks like it is already there. The length is checked against
what the server said while the answer is still at hand; this was found when a
real download arrived 170 KB short and the failure only surfaced two steps
later.

### Nothing on the card may need an FPU

*This is what stopped iGame launching games.* It listed them correctly and then
did nothing when one was clicked - window closed, WHDLoad never started, nothing
reported. With the FPU libraries off the card and `no_guigfx=1` in its
preferences, it launches.


Emu68 gives a PiStorm a **68040 with no FPU**. A floating point instruction on
such a machine raises a line-F exception - **guru 8000000B** - and iGame's own
site warns about exactly that guru for exactly these libraries.

Counting F-line opcodes in the binaries settles it, with the published no-FPU
build of `guigfx` as the control:

| Library | FPU instructions |
| --- | --- |
| `guigfx.library` (standard) | 41 |
| `guigfx.library` (no-FPU build) | 0 |
| `render.library` | **153** |

There is a no-FPU `guigfx` on Aminet and **no no-FPU `render` anywhere**, and
`guigfx.library` opens `render.library`, so the whole stack is unusable here.
iGame lists all three as optional, so the card does without them and iGame is
installed with `no_guigfx=1` in its own preferences. It loses the screenshots
and keeps working. PiMiga's copy of that preferences file had the same line in
it, which suggests somebody else met this years ago.

### MUI, and the classes that are not in MUI

MUI is published on Aminet, and what it publishes is MUI 3.8 with 36 classes.
A ready-made distribution's MUI is usually the richer one - PiMiga's carries 84
- which is what made mining one so tempting, and why the classes iGame needs
are named and fetched individually instead.

Those extra classes are not decoration. iGame's window is built from `NList`,
`NListview`, `TextEditor` and `Guigfx`, **none of which are part of MUI**, and
they are published separately. Each is now a package of its own, so a card built
from floppies and Aminet alone - no donor anywhere - has everything iGame opens:

| Package | Supplies |
| --- | --- |
| MUI | `muimaster.library` and 36 standard classes |
| MUI NList classes | `NList.mcc`, `NListview.mcc` and the rest of that family |
| MUI TextEditor class | `TextEditor.mcc` |
| MUI Guigfx class | `Guigfx.mcc`, `guigfx.library`, `render.library` |

iGame names all four as requirements, so ticking iGame ticks them.

### What a choice drags along with it

Ticking a package switches on what it requires, and that happens for a package
ticked by **default** too - iGame is on to begin with, and its MUI classes were
shown switched off beside it. They were installed anyway; the page simply did
not say so, and turning iGame off and on again appeared to "fix" it.

Untricking works the other way, with one distinction that matters:

* anything that **required** what was turned off goes with it - a MUI program
  without MUI is not a program;
* a package that was **only ever there to satisfy something else** goes when the
  last thing needing it goes. The MUI `NList`, `TextEditor` and `UrlText`
  classes are marked that way: nobody chooses them for their own sake;
* a package **worth having on its own stays**. Turning off one MUI program does
  not take MUI away from the others.


## iGame comes from its own release, and builds its own list

A donor's copy of a program is whatever its author installed. PiMiga's iGame is
v2.1 from June 2022, and it arrives with that person's `gameslist.csv` - an
absolute path to every slave on *their* machine - their screenshots and their
settings. Editing that list to match this card, which is what this tool used to
do, is guessing at another program's database.

So iGame is installed standalone from its current Aminet release: nothing from a
donor at all. The archive ships one binary per processor, and since Emu68 gives
a PiStorm a 68040, the `.040` build is installed under the name the icon
launches. The card gets an iGame with **no games list**, and the first thing to
do on the Amiga is *Settings > Game Repositories*, then *Actions > Scan
Repositories*: the paths are then ones iGame resolved from the drives in front
of it, and cannot disagree with what is there.

It still needs **MUI**, which no download here supplies - its window is built
from MUI classes (`NList`, `NListview`, `Guigfx`, `TextEditor`) that come with a
donor's MUI installation.

**A caution, stated because it is not fixed.** On the machine this was developed
against, iGame lists games correctly and then does nothing when one is clicked:
its window closes and WHDLoad never starts. That was reproduced with v2.1 and
v2.6.1, with the donor's list and with one iGame scanned for itself, with and
without WHDLoad requesters, on the full package set and on a card carrying
almost nothing. SnoopDos shows iGame reading the game's drawer successfully and
never asking the system to execute anything. It is not understood.

Games launch perfectly from **their own Workbench icons** in the Games drawer -
each is a project icon whose default tool is WHDLoad, and Workbench sets the
current directory to the game's drawer, which is the condition a WHDLoad slave
needs. `WHDLoad` given a full path from a shell, without that directory, fails
with `DOS-Error #205`.

## An installer that edits the boot script is not worth the icons

MagicWB is not offered at all any more. It is an eight-colour icon set that
suited an ECS machine perfectly, but it can no longer be registered or
supported, and its **Installer** had already earned its withdrawal: it
prepends two lines to `S:User-Startup`, one of which runs `MagicWB-Demon` to
claim pens 4 to 8, and a card it had been run on stopped booting with a
software error before Workbench appeared. That could not be reproduced in an
emulator - the same lines and the same Demon boot perfectly in FS-UAE - which
was a reason to keep it off a card rather than a reason to doubt it. For a
while its fonts and desktop patterns were installed with the Installer
withheld; unsupported and unregistrable, it has now left the catalogue, and
the build's own drawer icons come from the Workbench disks instead.

The general rule this belongs to: **anything that edits `S:User-Startup` or
`S:Startup-Sequence` on the Amiga can stop the card booting, and the user is
then a long way from a keyboard that can fix it.** Where this tool can do the
same work itself it does, and writes those lines with the rest of the startup
it already manages.

## A socket library belongs to its stack, not to the card

`bsdsocket.library` is the one library that must never be copied because a
program mentions it. Every browser and FTP client on a donor system names it,
so the dependency scanner copied it onto every card - including cards built
only for games. The file on the donor turned out to be an **AmiTCP 4.1 stub
from 1996** with no AmiTCP daemon anywhere behind it, and its mere presence in
`LIBS:` killed every WHDLoad game: a yellow screen, then nothing. Bisecting a
card down to that single file, against a card proven to run the game, is what
found it. It is now in `NEVER_SCAVENGE` along with `usergroup` and `ixnet`.

A stack puts its own socket library in place, so nothing is lost by refusing to
guess at one:

- **MiamiDx**, which the PiMiga donor carries, publishes `bsdsocket.library` in
  memory when it goes online and ships no copy on disk. The `network` package
  installs it with the `Miami:` assign and MUI that it needs - though Miami
  arrives unregistered and unconfigured, and has to be set up on the Amiga.
- **Roadshow** installs a real `bsdsocket.library` into `LIBS:`. On a card that
  also runs games, `C:NetShutdown` in `S:WHDLoad-Startup` takes the stack down
  while a game runs, which is what those WHDLoad hooks are for.

APC&TCP serve the Roadshow demo only to a browser, so the `roadshow` package is
declared as a download this tool cannot make: put `Roadshow-Demo-1.15.lha` in
`~/.cache/pistorm-imager/packages` and the build uses it, and when it is absent
the build says where to get it instead of leaving the card silently stackless.

The archive is an installer distribution, not a Workbench disk: the part that
is shaped like one sits in a `Workbench` drawer beside the documentation, the
`Install_Roadshow` script and the publisher's `Installer`. So the merge looks
one level in, places `C`, `Libs`, `Devs`, `S`, `Locale` and `Storage` from
there, and stages the rest in `Storage/Install/Roadshow`. Two details matter:

- **`S/User-Startup` is never placed.** Roadshow's copy is four lines meant to
  be *added* to the card's, and placing it as a file would either overwrite
  everything the build wrote there or be skipped, leaving the stack unstarted.
  The lines go in through the same `startup` mechanism every other package
  uses.
- **The card is given an interface for the machine it is being built for.**
  Every one of the fifty-odd templates in `Storage/NetInterfaces` is for
  somebody else's hardware - A2065, X-Surf, Ariadne - so the build writes
  `DEVS:NetInterfaces/vlink` naming `vlink.device`, which is what a PiStorm
  has, and asks for DHCP. Without it `AddNetInterface` has nothing to bring
  up and the stack installs but never runs.

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

These fixes apply to **everything that goes on the system drive** - the floppies,
the packages, and a directory-based drive from an emulator such as PiMiga's
`disks/System`. Two details make that work, and both were wrong for a long time:
the pass is shown the path a file will have *on the card* rather than where it
sits in the thing being copied, so a rule naming `Storage` or `Libs/Picasso96`
can match at all; and it decides once, when the volume is full, rather than
after each tree copied - deciding after the first meant deciding before any
package had been installed. The fixes are:

* the emulator's RTG driver (`uaegfx.card`) is dropped and Emu68's
  `VideoCore.card` installed in `LIBS:Picasso96/` in its place;
* where a system being adapted already had a Picasso96 monitor for the
  emulator's board, it is written out as `VideoCore` with `BOARDTYPE=VideoCore`
  in its icon, which is how Picasso96 chooses its board;
* a Picasso96 that was *chosen as a package* counts as installed even before
  anything is copied, while a copy merely staged in `Storage/Install` for you
  to install later does not - staging is not installing;

A card built from floppies is **not** given a monitor file, and this is
deliberate. Making one by renaming the emulator's monitor looked right - every
Picasso96 monitor is the same loader with its board named in its icon - and
produced a card that would not boot: a software error in VideoCore, which is
that monitor bringing the board up against a 1999 `rtg.library`. Emu68's
`VideoCore.card` still goes on the card, where nothing loads it until a monitor
names it, and the monitor is left to Picasso96's own installer in
`Storage/Install`, which is the only thing that knows what it is installing
against. Without one the board's screen modes cannot be selected, so this is a
gap rather than a fix - but a card that boots and cannot use RTG is worth more
than one that does not boot.
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
