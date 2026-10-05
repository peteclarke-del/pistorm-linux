# Which AmigaOS the app suggests

The **Suggested for this machine** row on the System step, worked out by
`pistorm_imager/core/suggest.py`. This table is what that code answers
(version 0.20.3) for every machine the app supports: with no accelerator,
with an accelerator of each processor, and with a PiStorm. Redraw it with
`python3 tests/os_suggestions.py`.

**Assumed for every row:** every release's media is to hand - the AmigaOS 3.2,
3.5 and 3.9 CDs and the Workbench 1.3, 2.0, 2.1, 3.0 and 3.1 floppies - and the
Kickstart files are the ones in `samples/kickstart`. With media missing the
suggestion is the same, says what is missing, and offers the best of what is
there as well. The screen makes no difference: an RTG screen only rules out
releases older than 3.0, and those never come first anyway.

## How it ranks

- **The Kickstart decides, where it is a chip.** Without a PiStorm, the ROM
  chosen on the Machine step is the one fitted in the machine. Only releases
  that run on it are suggested, and one **made for** it comes first, newest
  first: 3.9 on a 3.1 chip with a 68020, 3.2 on a 3.2 chip. One that only runs
  on it by loading its own modules over it at every boot - 3.2 on a 3.1 chip,
  the pairing with a documented crash - comes after, and says so.
- **On a PiStorm the Kickstart is a file**, so any release's own ROM can be
  had: the newest release that runs, on its own ROM.
- **No ROM given, without a PiStorm:** the newest release that runs with an
  accelerator, the newest on a Kickstart the machine takes as its own without
  one - and the suggestion asks for the ROM, since the chip decides.
- **The processor:** 3.5 and 3.9 need a 68020; 3.2 and the floppy releases run
  on a 68000.

## The matrix

"Kickstart" is the chip fitted, or the PiStorm's file. "Then" is the rest of
the ranking, best first.

| Machine | Accelerator | Kickstart | Suggested | Then |
| --- | --- | --- | --- | --- |
| Amiga 500 | None - stock 68000 | 40.63 chip | **Workbench 3.1** | OS 3.2 |
| Amiga 500 | None - stock 68000 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 | Accelerator, 68020 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500 | Accelerator, 68020 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 | Accelerator, 68030 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500 | Accelerator, 68030 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 | Accelerator, 68040 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500 | Accelerator, 68040 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 | Accelerator, 68060 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500 | Accelerator, 68060 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 | PiStorm (Emu68, 68040) | a file on the SD card | **AmigaOS 3.2** | OS 3.9, OS 3.5, WB 3.1, WB 2.1, WB 3.0, WB 2.0, WB 1.3 |
| Amiga 500 with ECS | None - stock 68000 | 40.63 chip | **Workbench 3.1** | OS 3.2 |
| Amiga 500 with ECS | None - stock 68000 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 with ECS | Accelerator, 68020 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500 with ECS | Accelerator, 68020 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 with ECS | Accelerator, 68030 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500 with ECS | Accelerator, 68030 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 with ECS | Accelerator, 68040 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500 with ECS | Accelerator, 68040 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 with ECS | Accelerator, 68060 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500 with ECS | Accelerator, 68060 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500 with ECS | PiStorm (Emu68, 68040) | a file on the SD card | **AmigaOS 3.2** | OS 3.9, OS 3.5, WB 3.1, WB 2.1, WB 3.0, WB 2.0, WB 1.3 |
| Amiga 500+ | None - stock 68000 | 40.63 chip | **Workbench 3.1** | OS 3.2 |
| Amiga 500+ | None - stock 68000 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500+ | Accelerator, 68020 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500+ | Accelerator, 68020 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500+ | Accelerator, 68030 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500+ | Accelerator, 68030 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500+ | Accelerator, 68040 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500+ | Accelerator, 68040 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500+ | Accelerator, 68060 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 500+ | Accelerator, 68060 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 500+ | PiStorm (Emu68, 68040) | a file on the SD card | **AmigaOS 3.2** | OS 3.9, OS 3.5, WB 3.1, WB 2.1, WB 3.0, WB 2.0, WB 1.3 |
| Amiga 600 | None - stock 68000 | 40.63 chip | **Workbench 3.1** | OS 3.2 |
| Amiga 600 | None - stock 68000 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 600 | Accelerator, 68020 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 600 | Accelerator, 68020 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 600 | Accelerator, 68030 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 600 | Accelerator, 68030 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 600 | Accelerator, 68040 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 600 | Accelerator, 68040 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 600 | Accelerator, 68060 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 600 | Accelerator, 68060 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 600 | PiStorm (Emu68, 68040) | a file on the SD card | **AmigaOS 3.2** | OS 3.9, OS 3.5, WB 3.1, WB 2.1, WB 3.0, WB 2.0, WB 1.3 |
| Amiga 1000 | None - stock 68000 | 40.63 chip | **Workbench 3.1** | OS 3.2 |
| Amiga 1000 | None - stock 68000 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1000 | Accelerator, 68020 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 1000 | Accelerator, 68020 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1000 | Accelerator, 68030 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 1000 | Accelerator, 68030 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1000 | Accelerator, 68040 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 1000 | Accelerator, 68040 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1000 | Accelerator, 68060 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 1000 | Accelerator, 68060 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1000 | PiStorm (Emu68, 68040) | a file on the SD card | **AmigaOS 3.2** | OS 3.9, OS 3.5, WB 3.1, WB 2.1, WB 3.0, WB 2.0, WB 1.3 |
| Amiga 2000 | None - stock 68000 | 40.63 chip | **Workbench 3.1** | OS 3.2 |
| Amiga 2000 | None - stock 68000 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 2000 | Accelerator, 68020 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 2000 | Accelerator, 68020 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 2000 | Accelerator, 68030 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 2000 | Accelerator, 68030 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 2000 | Accelerator, 68040 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 2000 | Accelerator, 68040 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 2000 | Accelerator, 68060 | 40.63 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 2000 | Accelerator, 68060 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 2000 | PiStorm (Emu68, 68040) | a file on the SD card | **AmigaOS 3.2** | OS 3.9, OS 3.5, WB 3.1, WB 2.1, WB 3.0, WB 2.0, WB 1.3 |
| Amiga 1200 | None - stock 68020 | 40.68 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 1200 | None - stock 68020 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1200 | Accelerator, 68020 | 40.68 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 1200 | Accelerator, 68020 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1200 | Accelerator, 68030 | 40.68 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 1200 | Accelerator, 68030 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1200 | Accelerator, 68040 | 40.68 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 1200 | Accelerator, 68040 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1200 | Accelerator, 68060 | 40.68 chip | **AmigaOS 3.9** | OS 3.5, WB 3.1, OS 3.2 |
| Amiga 1200 | Accelerator, 68060 | 47.96 chip | **AmigaOS 3.2** | - |
| Amiga 1200 | PiStorm (Emu68, 68040) | a file on the SD card | **AmigaOS 3.2** | OS 3.9, OS 3.5, WB 3.1, WB 2.1, WB 3.0, WB 2.0, WB 1.3 |
| Raspberry Pi on its own | Emu68 (68040) | a file on the SD card | **AmigaOS 3.2** | OS 3.9, OS 3.5, WB 3.1, WB 2.1, WB 3.0, WB 2.0, WB 1.3 |
