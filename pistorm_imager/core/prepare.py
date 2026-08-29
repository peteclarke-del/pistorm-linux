"""Doing the network-facing preparation before a privileged write starts.

The GUI calls this as the ordinary user; the result is a directory of Emu68
files that :mod:`pistorm_imager.core.builder` can install without touching the
network, so the part of the run that needs root stays offline and local.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import builder, emu68
from .util import Progress


def stage_emu68(config: builder.BuildConfig, progress: Progress) -> Path | None:
    """Download and unpack the chosen Emu68 build into a temporary directory."""
    if not config.install_emu68 or config.emu68_prepared_dir:
        return None
    staging = Path(tempfile.mkdtemp(prefix="pistorm-emu68-"))
    files, root = builder._prepare_emu68(  # noqa: SLF001 - one deliberate re-use
        builder.BuildConfig(
            variant=config.variant,
            release_tag=config.release_tag,
            emu68_archive=config.emu68_archive,
        ),
        staging,
        progress,
    )
    return root
