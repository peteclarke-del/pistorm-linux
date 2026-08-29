"""Small shared helpers: size parsing/formatting, subprocess running, progress."""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
import sys
import time
from typing import Callable, Iterable, Optional

SECTOR = 512
KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB


def human_size(n: int) -> str:
    """Format a byte count the way disk tools do (1 GiB -> '1.00 GiB')."""
    for unit, div in (("TiB", GIB * 1024), ("GiB", GIB), ("MiB", MIB), ("KiB", KIB)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def parse_size(text: str) -> int:
    """Parse '512M', '1.5G', '2048' (MiB assumed for bare numbers) into bytes."""
    text = text.strip().upper().replace("IB", "").replace(" ", "")
    if not text:
        raise ValueError("empty size")
    mult = MIB
    if text[-1] in "BKMGT":
        mult = {"B": 1, "K": KIB, "M": MIB, "G": GIB, "T": GIB * 1024}[text[-1]]
        text = text[:-1]
    return int(float(text) * mult)


def align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


class CommandError(RuntimeError):
    def __init__(self, argv, returncode, output):
        self.argv = argv
        self.returncode = returncode
        self.output = output
        super().__init__(
            f"{argv[0]} failed with exit code {returncode}:\n{output.strip()}"
        )


def run(argv: list[str], *, check: bool = True, input_text: str | None = None,
        log: Optional[Callable[[str], None]] = None) -> str:
    """Run a command, capturing combined output. Raises CommandError on failure."""
    if log:
        log("$ " + " ".join(argv))
    proc = subprocess.run(
        argv,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if log and proc.stdout:
        for line in proc.stdout.rstrip().splitlines():
            log("  " + line)
    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, proc.stdout or "")
    return proc.stdout or ""


def require_tool(name: str, package_hint: str = "") -> str:
    path = shutil.which(name)
    if path is None:
        hint = f" (install the '{package_hint}' package)" if package_hint else ""
        raise RuntimeError(f"Required tool '{name}' was not found on PATH{hint}.")
    return path


@dataclasses.dataclass
class Progress:
    """Progress sink shared by the build steps.

    ``on_step`` reports which named stage we are in, ``on_fraction`` a 0..1
    completion for the current stage, and ``on_log`` free-form text for the log
    view.  The GUI supplies thread-safe callbacks; the CLI prints to stderr.
    """

    on_step: Callable[[str], None] = lambda text: None
    on_fraction: Callable[[float], None] = lambda frac: None
    on_log: Callable[[str], None] = lambda text: None
    cancelled: Callable[[], bool] = lambda: False

    def step(self, text: str) -> None:
        self.on_step(text)
        self.on_log(f"== {text}")

    def log(self, text: str) -> None:
        self.on_log(text)

    def fraction(self, frac: float) -> None:
        self.on_fraction(max(0.0, min(1.0, frac)))

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise Cancelled()


class Cancelled(Exception):
    """Raised inside a build when the user asks to stop."""


def console_progress() -> Progress:
    """A Progress that writes to stderr, for the command line entry point."""
    state = {"last": 0.0}

    def on_fraction(frac: float) -> None:
        now = time.monotonic()
        if now - state["last"] < 0.2 and frac < 1.0:
            return
        state["last"] = now
        width = 40
        filled = int(frac * width)
        sys.stderr.write("\r[" + "#" * filled + "." * (width - filled) + f"] {frac*100:5.1f}%")
        sys.stderr.flush()
        if frac >= 1.0:
            sys.stderr.write("\n")

    return Progress(
        on_step=lambda t: sys.stderr.write(f"\n>> {t}\n"),
        on_fraction=on_fraction,
        on_log=lambda t: sys.stderr.write(f"   {t}\n"),
    )


def write_all(dst, data) -> None:
    """Write every byte of ``data``.

    ``write`` on an unbuffered file object is allowed to consume only part of
    the buffer and report how much it took.  Ignoring that return value is how
    an image ends up quietly truncated, so loop until the buffer is empty.
    """
    view = memoryview(data)
    while view:
        written = dst.write(view)
        if written is None:  # buffered objects consume everything or raise
            return
        if written == 0:
            raise OSError("write returned 0 bytes; the target may be full")
        view = view[written:]


def copy_stream(src, dst, total: int | None, progress: Progress,
                chunk: int = 4 * MIB, limit: int | None = None) -> int:
    """Copy between file objects, reporting progress and honouring cancellation.

    ``limit`` stops after that many bytes, which is how a single partition is
    lifted out of a much larger card image.
    """
    done = 0
    while True:
        progress.check_cancelled()
        want = chunk if limit is None else min(chunk, limit - done)
        if want <= 0:
            break
        buf = src.read(want)
        if not buf:
            break
        write_all(dst, buf)
        done += len(buf)
        if total:
            progress.fraction(done / total)
    return done
