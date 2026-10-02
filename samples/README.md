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

`pfs3aio` can also be lifted automatically out of any hard disk image whose
Rigid Disk Block already contains a PFS3 handler, so you may not need to find
one separately.

An AmigaOS 3.2, 3.5 or 3.9 CD image is not looked for here: it is chosen in the
window, on the System step, and can live wherever you keep it. A 3.2 disc brings
Kickstart ROMs of its own, which a PiStorm card is given when no other has been
chosen.

The tests that use these files skip when they are absent.
