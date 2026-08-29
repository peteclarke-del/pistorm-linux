"""Serialising a :class:`BuildConfig` so it can cross a privilege boundary.

Writing to a real SD card needs root, but the GUI must not run as root.  The GUI
therefore writes the job to a file and re-executes just the builder under
``pkexec``; this module is the contract between the two halves, and is also what
the "save/load settings" feature stores.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from . import bootcfg, builder

FORMAT_VERSION = 1


def to_dict(config: builder.BuildConfig) -> dict:
    data = dataclasses.asdict(config)
    data["mode"] = config.mode.value
    return {"version": FORMAT_VERSION, "config": data}


def from_dict(payload: dict) -> builder.BuildConfig:
    if payload.get("version") != FORMAT_VERSION:
        raise ValueError(f"unsupported job format version {payload.get('version')!r}")
    data = dict(payload["config"])
    data["mode"] = builder.BuildMode(data["mode"])
    data["boot_options"] = bootcfg.BootOptions(**data.get("boot_options", {}))
    for field in ("amiga_partitions", "extra_partitions"):
        data[field] = [builder.AmigaPartitionSpec(**spec)
                       for spec in data.get(field, [])]
    known = {f.name for f in dataclasses.fields(builder.BuildConfig)}
    unknown = set(data) - known
    for key in unknown:
        data.pop(key)
    return builder.BuildConfig(**data)


def save(config: builder.BuildConfig, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(config), indent=2), encoding="utf-8")
    return path


def load(path: str | Path) -> builder.BuildConfig:
    return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------- sessions

def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / "pistorm-imager"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_file() -> Path:
    return config_dir() / "session.json"


def save_session(config: builder.BuildConfig, interface: dict,
                 path: str | Path | None = None) -> Path:
    """Save a build together with the choices that led to it.

    A :class:`BuildConfig` records what to build but not how the interface got
    there - which Amiga, which display, which folders - so saving it alone
    would still leave the hardware to be picked again every time.
    """
    path = Path(path) if path is not None else session_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = to_dict(config)
    payload["interface"] = interface
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_session(path: str | Path | None = None) -> tuple[builder.BuildConfig, dict]:
    """Load a saved build and its interface choices.

    Files written before the interface section existed still load; they simply
    return no choices.
    """
    path = Path(path) if path is not None else session_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return from_dict(payload), payload.get("interface") or {}


def have_session() -> bool:
    return session_file().is_file()
