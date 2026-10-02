# samples

The imager looks in this folder for the Amiga files it needs. Nothing here is
kept in the repository: Kickstart ROMs, Workbench floppy images and Cloanto's
`rom.key` are licensed material, and the PFS3 and Picasso96 handlers belong to
their own authors. Supply your own copies.

Put them here and the tool finds them on its own:

```
samples/
  kickstart/
    <an A1200 Kickstart ROM>      e.g. "KS ROM v3.1 (A1200) rev 40.68 (512k).rom"
    <other Kickstart ROMs>        any you have: 1.3 is the one most WHDLoad
                                  games want
    rom.key                       only for Cloanto encrypted ROMs
  workbench/
    <the Workbench floppy images> Install, Workbench, Extras, Storage, Locale, Fonts
  drivers/
    A1200/scsi.device             AmigaOS 3.2's IDE driver, for drives past 4 GB
    A600/scsi.device              the same file, for an A600
  pfs3aio                         the PFS3 handler, embedded in the RDB
  rtg.library                     optional; a known-good copy used if a source
                                  file cannot be read
```

ROMs are recognised by their contents, not their file names: the version in
the header, and the machine from the checksum or, failing that, the models the
file names. The folder is also where WHDLoad's Kickstart images are taken from
unless another is chosen under WHDLoad on the Software step - every ROM there
that WHDLoad has a relocation table for is copied to `Devs/Kickstarts` under
the name it looks for.

Disks are recognised by the volume name *inside* them rather than by file name,
so however your collection is named it should be picked up. A verified GoodTools
dump (`[!]`) is preferred over a modified one where both are present.

`drivers/` holds the IDE driver an A600 or A1200 needs to reach past the
first 4 GB of a CF card or disk on its own IDE port. The `scsi.device` in a
Kickstart older than 3.1.4 cannot, so every drive beyond that line comes up
"not formatted". The driver here is AmigaOS 3.2's - `IDE_scsidisk 47.4
(30.12.2019)`, 15,876 bytes, the same file for both machines - taken from
`DEVS/A1200` on the 3.2 CD's `ModulesA1200_3.2.adf` (`ModulesA600_3.2.adf`
has an identical copy). It is Hyperion's, which is why it lives here with the
ROMs rather than being downloaded.

The drawer it is in names the machine it is for, the way LoadModule itself
lays them out, so a file is never guessed to suit a model. When a drive needs
it, the build puts every model's copy in `Devs/<model>` on the system drive,
adds LoadModule (from Aminet) to `C:`, and runs `C:LoadModule AUTO` at the top
of `S:Startup-Sequence`: LoadModule loads the copy for the machine it finds
itself on and restarts once, and from then on the drive is read with the new
driver. An AmigaOS 3.2 install from the CD brings its own and does not need
this. Like the PFS3 handler, a copy found here is kept in the application's
cache, so the installed application finds it too; `~/Amiga` is searched the
same way.

Proved in FS-UAE on the A1200's own Kickstart 3.1 with Workbench 3.1: PFS3
drives from 3.8 GB to 16 GB were unreadable without it and all mounted with it.

`pfs3aio` can also be lifted automatically out of any hard disk image whose
Rigid Disk Block already contains a PFS3 handler, so you may not need to find
one separately.

An AmigaOS 3.2, 3.5 or 3.9 CD image is not looked for here: it is chosen in the
window, on the System step, and can live wherever you keep it. A 3.2 disc brings
Kickstart ROMs of its own, which a PiStorm card is given when no other has been
chosen.

The tests that use these files skip when they are absent.
