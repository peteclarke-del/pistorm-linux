# Which AmigaOS the app suggests

The **Suggested for this machine** row on the System step, worked out by
`pistorm_imager/core/suggest.py`. This table is what that code answers today
(version 0.20.3) for every machine the app supports, with no accelerator,
with an accelerator of each processor, and with a PiStorm.

**Assumed for every row:** every release's media is to hand - the AmigaOS 3.2,
3.5 and 3.9 CDs and the Workbench 1.3, 2.0, 2.1, 3.0 and 3.1 floppies - and the
Kickstart files are the ones in `samples/kickstart` (3.1 and 3.2, for the A1200
and for the A500/A600/A2000). With media missing, the suggestion is the same
and says what is missing, and a second button offers the best of what is
there. The screen makes no difference to the result: an RTG screen only rules
out releases older than 3.0, and those never come first anyway.

## How it ranks

- **A PiStorm or an accelerator:** the newest release, by year, that runs on
  the processor. 3.5 and 3.9 need a 68020; 3.2 and the floppy releases run on
  a 68000.
- **No accelerator:** the newest release on a Kickstart the machine lists as
  its own.
- **The Kickstart:** the release's best first (3.2 on its own 3.2 ROM), then
  one built for the machine's chipset, then the newest revision.

## The matrix

"Next" is the rest of the ranking, best first.

| Machine | Accelerator | Suggested | Kickstart | Next | Note |
| --- | --- | --- | --- | --- | --- |
| Amiga 500 | None - stock 68000 | **Workbench 3.1** | Kickstart 3.1 A500/A600/A2000 (40.63) | WB 2.1, WB 2.0, OS 3.2 | Runs on the machine's own Kickstart 40 chip |
| Amiga 500 | Accelerator, 68020 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500 | Accelerator, 68030 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500 | Accelerator, 68040 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500 | Accelerator, 68060 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500 | PiStorm (Emu68, 68040) | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the file on the SD card |
| Amiga 500 with ECS | None - stock 68000 | **Workbench 3.1** | Kickstart 3.1 A500/A600/A2000 (40.63) | WB 2.1, WB 2.0, OS 3.2 | Runs on the machine's own Kickstart 40 chip |
| Amiga 500 with ECS | Accelerator, 68020 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500 with ECS | Accelerator, 68030 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500 with ECS | Accelerator, 68040 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500 with ECS | Accelerator, 68060 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500 with ECS | PiStorm (Emu68, 68040) | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the file on the SD card |
| Amiga 500+ | None - stock 68000 | **Workbench 3.1** | Kickstart 3.1 A500/A600/A2000 (40.63) | WB 2.1, WB 2.0, OS 3.2 | Runs on the machine's own Kickstart 40 chip |
| Amiga 500+ | Accelerator, 68020 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500+ | Accelerator, 68030 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500+ | Accelerator, 68040 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500+ | Accelerator, 68060 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 500+ | PiStorm (Emu68, 68040) | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the file on the SD card |
| Amiga 600 | None - stock 68000 | **Workbench 3.1** | Kickstart 3.1 A500/A600/A2000 (40.63) | OS 3.2, WB 2.1, WB 3.0 | Runs on the machine's own Kickstart 40 chip |
| Amiga 600 | Accelerator, 68020 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 600 | Accelerator, 68030 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 600 | Accelerator, 68040 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 600 | Accelerator, 68060 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 600 | PiStorm (Emu68, 68040) | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the file on the SD card |
| Amiga 1000 | None - stock 68000 | **Workbench 3.1** | Kickstart 3.1 A500/A600/A2000 (40.63) | OS 3.2, WB 2.1, WB 3.0 | Runs on the machine's own Kickstart 40 chip |
| Amiga 1000 | Accelerator, 68020 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 1000 | Accelerator, 68030 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 1000 | Accelerator, 68040 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 1000 | Accelerator, 68060 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 1000 | PiStorm (Emu68, 68040) | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the file on the SD card |
| Amiga 2000 | None - stock 68000 | **Workbench 3.1** | Kickstart 3.1 A500/A600/A2000 (40.63) | OS 3.2, WB 2.1, WB 3.0 | Runs on the machine's own Kickstart 40 chip |
| Amiga 2000 | Accelerator, 68020 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 2000 | Accelerator, 68030 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 2000 | Accelerator, 68040 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 2000 | Accelerator, 68060 | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 2000 | PiStorm (Emu68, 68040) | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the file on the SD card |
| Amiga 1200 | None - stock 68020 | **AmigaOS 3.2** | Kickstart 3.2 A1200 (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 1200 | Accelerator, 68020 | **AmigaOS 3.2** | Kickstart 3.2 A1200 (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 1200 | Accelerator, 68030 | **AmigaOS 3.2** | Kickstart 3.2 A1200 (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 1200 | Accelerator, 68040 | **AmigaOS 3.2** | Kickstart 3.2 A1200 (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 1200 | Accelerator, 68060 | **AmigaOS 3.2** | Kickstart 3.2 A1200 (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the chip: needs a 3.2 ROM fitted, or 3.2's modules soft-loaded over a 3.1 chip at every boot |
| Amiga 1200 | PiStorm (Emu68, 68040) | **AmigaOS 3.2** | Kickstart 3.2 A1200 (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the file on the SD card |
| Raspberry Pi on its own | Emu68 (68040) | **AmigaOS 3.2** | Kickstart 3.2 A500/A600/A1000/A2000/CDTV (47.96) | OS 3.9, OS 3.5, WB 3.1 | Kickstart is the file on the SD card |

## What this shows

- **Every accelerated machine gets AmigaOS 3.2, never 3.9.** The ranking with
  an accelerator is by year alone, so 3.2 (2021) always beats 3.9 (2000).
- **On a machine without a PiStorm that is not a free choice.** The Kickstart
  is the chip in the machine, and 3.2 is best on its own 3.2 ROM. On a 3.1
  chip it runs by soft-loading its ROM modules at every boot - the combination
  with a documented crash - while 3.9 runs on the 3.1 chip as it is. Every
  accelerated row without a PiStorm is one of those.
- **On a PiStorm the ranking holds.** The Kickstart is a file on the SD card,
  so the 3.2 ROM costs nothing.
- **A stock 68000 gets Workbench 3.1** on its own 3.1 ROM; the A1200, whose
  stock processor is a 68020, is ranked as accelerated.
- **The bare Raspberry Pi** has no Amiga ROM chip at all, and is treated like
  a PiStorm.
- **The machine data is uneven about 3.2 ROMs.** The A1200 lists Kickstart 3.2
  among its own ROMs, but the A500, A500+, A600, A1000 and A2000 do not, though
  Hyperion makes a 3.2 ROM for them too. It does not change a row here, since
  the 68000 rule only applies to machines without an accelerator, but it is
  why "a Kickstart the machine lists as its own" is not yet a sound way to
  judge a ROM chip.
