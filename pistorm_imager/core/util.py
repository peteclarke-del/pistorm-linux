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
    """Parse a size, distinguishing the two meanings of "GB".

    This matters more than it looks. A card sold as 32 GB holds 32 thousand
    million bytes, which is 29.8 GiB - so an image built as "32 GiB" is over
    two gigabytes too big to fit the card it was meant for.

    ``32GB`` is decimal (10^9), ``32GiB`` binary (2^30), and a bare ``32G`` is
    binary by the usual convention. A number on its own is MiB.
    """
    text = text.strip().upper().replace(" ", "")
    if not text:
        raise ValueError("empty size")

    binary = {"K": KIB, "M": MIB, "G": GIB, "T": GIB * 1024}
    decimal = {"K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4}

    unit, table = "M", binary
    if text.endswith("IB") and len(text) > 3 and text[-3] in binary:
        unit, text = text[-3], text[:-3]
    elif text.endswith("B") and len(text) > 2 and text[-2] in decimal:
        unit, table, text = text[-2], decimal, text[:-2]
    elif text[-1] in binary:
        unit, text = text[-1], text[:-1]
    elif text.endswith("B"):
        unit, table, text = "B", {"B": 1}, text[:-1]

    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"cannot make sense of the size {text!r}") from None
    if value < 0:
        raise ValueError("a size cannot be negative")
    return int(value * table.get(unit, MIB))


def describe_size(count: int) -> str:
    """Both readings of a size, so neither can be mistaken for the other."""
    return f"{human_size(count)} ({count / 1000 ** 3:.2f} GB as cards are sold)"


def fits_card(image_bytes: int, card_gb: float) -> bool:
    """Whether an image fits a card of the advertised capacity."""
    return image_bytes <= card_gb * 1000 ** 3


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
