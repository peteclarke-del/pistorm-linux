#!/usr/bin/env python3
"""Command line front end - also how the GUI performs privileged writes.

When the target is a real SD card the GUI re-runs ``build`` under ``pkexec`` with
``--progress-json``, and reads progress back off stdout one JSON object per line.
The same commands are perfectly usable by hand for a headless build.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow "python3 cli.py", which pkexec needs
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pistorm_imager.core import (builder, devices, emu68, hdfcheck, jobs,  # noqa: E402
                                 kickstart, rdb)
from pistorm_imager.core.util import Cancelled, Progress, console_progress, human_size  # noqa: E402


def json_progress() -> Progress:
    def emit(kind: str, value) -> None:
        sys.stdout.write(json.dumps({"type": kind, "value": value}) + "\n")
        sys.stdout.flush()

    return Progress(
        on_step=lambda text: emit("step", text),
        on_fraction=lambda frac: emit("fraction", frac),
        on_log=lambda text: emit("log", text),
    )


def cmd_list_devices(args) -> int:
    found = devices.list_devices(only_removable=not args.all)
    if args.json:
        print(json.dumps([{
            "path": d.path, "size": d.size, "description": d.description,
            "removable": d.likely_sd_card, "holds_system": d.holds_system,
            "read_only": d.read_only, "mounts": d.mounted_paths,
        } for d in found], indent=2))
        return 0
    if not found:
        print("No removable devices found. Use --all to list every disk.")
        return 1
    for device in found:
        flag = "  [SYSTEM DISK - refused]" if device.holds_system else ""
        print(f"{device.description}{flag}")
    return 0


def cmd_releases(args) -> int:
    for release in emu68.fetch_releases(args.limit):
        assets = ", ".join(v.key for v in emu68.VARIANTS
                           if emu68.has_variant(release, v.key))
        print(f"{release.tag:<22} {release.published}  {release.display()}")
        print(f"{'':<22} boards: {assets or 'none'}")
    return 0


def cmd_inspect(args) -> int:
    config = builder.BuildConfig(
        mode=builder.BuildMode.CUSTOMISE,   # inspect never creates anything
        target=args.target,
        target_is_device=devices.is_block_device(args.target),
    )
    print(builder.describe_target(config))
    return 0


def cmd_identify(args) -> int:
    for path in args.roms:
        info = kickstart.identify(path, args.key)
        status = "usable" if info.usable else "NOT USABLE"
        print(f"{Path(path).name}: {info.name} [{status}] "
              f"{human_size(info.size)} sha1={info.sha1[:12]}")
        if info.note:
            print(f"    {info.note}")
        if info.version and not info.aga:
            print("    warning: not an AGA/A1200 ROM - Emu68 expects an A1200 Kickstart")
    return 0


def cmd_check(args) -> int:
    """Report - and optionally repair - PiStorm compatibility of an image."""
    info = builder.inspect_hdf(args.image)
    print(info.description)
    if info.table is None:
        if info.bare_dostype is not None:
            print("No Rigid Disk Block. This is a single bare partition; the "
                  "imager will build an RDB around it when writing a card.")
            return 0
        print("No RDB and no recognisable Amiga file system: this is not an "
              "Amiga hard disk image.", file=sys.stderr)
        return 1

    print()
    print(info.table.describe())
    print()
    capacity = args.capacity and int(args.capacity) or (info.source_length or info.size)
    findings = hdfcheck.analyse(info.table, capacity)
    for finding in findings:
        print(f"  {finding}")
    print(hdfcheck.summarise(findings))

    if not args.fix:
        return 1 if hdfcheck.blocking(findings) or findings else 0

    donors: list = []
    if args.donor:
        with open(args.donor, "rb") as handle:
            located = builder.find_rdb(handle)
        if located:
            donors = located[1].filesystems
            print(f"\n{len(donors)} handler(s) available from "
                  f"{Path(args.donor).name}")

    actions = hdfcheck.repair(info.table, capacity, donors)
    if not actions:
        print("\nNothing to repair.")
        return 0
    for action in actions:
        print(f"  fixed: {action}")
    with open(args.image, "r+b") as handle:
        info.table.write(handle, info.source_offset)
    print(f"\nRewrote the RDB in {args.image} with {len(actions)} correction"
          f"{'s' if len(actions) != 1 else ''}.")
    remaining = hdfcheck.analyse(info.table, capacity)
    if remaining:
        print("\nStill outstanding:")
        for finding in remaining:
            print(f"  {finding}")
        return 1
    return 0


def cmd_build(args) -> int:
    config = jobs.load(args.job)
    progress = json_progress() if args.progress_json else console_progress()
    try:
        builder.run_build(config, progress)
    except Cancelled:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - the GUI needs the message, not a trace
        if args.progress_json:
            sys.stdout.write(json.dumps({"type": "error", "value": str(error)}) + "\n")
            sys.stdout.flush()
        else:
            print(f"\nError: {error}", file=sys.stderr)
        return 1
    if args.progress_json:
        sys.stdout.write(json.dumps({"type": "done", "value": True}) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pistorm-imager",
        description="Prepare a PiStorm/Emu68 SD card on Linux.")
    sub = parser.add_subparsers(dest="command", required=True)

    devices_parser = sub.add_parser("list-devices", help="list candidate SD cards")
    devices_parser.add_argument("--all", action="store_true",
                                help="include non-removable disks")
    devices_parser.add_argument("--json", action="store_true")
    devices_parser.set_defaults(func=cmd_list_devices)

    releases_parser = sub.add_parser("releases", help="list Emu68 releases")
    releases_parser.add_argument("--limit", type=int, default=10)
    releases_parser.set_defaults(func=cmd_releases)

    inspect_parser = sub.add_parser("inspect", help="show a card's partitions and RDB")
    inspect_parser.add_argument("target")
    inspect_parser.set_defaults(func=cmd_inspect)

    identify_parser = sub.add_parser("identify", help="identify Kickstart ROM files")
    identify_parser.add_argument("roms", nargs="+")
    identify_parser.add_argument("--key", help="path to a Cloanto rom.key")
    identify_parser.set_defaults(func=cmd_identify)

    check_parser = sub.add_parser(
        "check", help="check an .hdf or card image for PiStorm compatibility")
    check_parser.add_argument("image")
    check_parser.add_argument("--fix", action="store_true",
                              help="apply the safe repairs to the image in place")
    check_parser.add_argument("--donor",
                              help="an .hdf or card image to take missing file "
                                   "system handlers from")
    check_parser.add_argument("--capacity", type=int,
                              help="bytes the drive will occupy (default: its "
                                   "own size)")
    check_parser.set_defaults(func=cmd_check)

    build_parser = sub.add_parser("build", help="run a saved build job")
    build_parser.add_argument("--job", required=True, help="job JSON written by the GUI")
    build_parser.add_argument("--progress-json", action="store_true",
                              help="emit machine readable progress on stdout")
    build_parser.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
