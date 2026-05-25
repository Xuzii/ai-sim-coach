"""Capture ACC's raw shared-memory pages to a ``.pwcap`` recording.

The first concrete step of the ACC reader (C3). It copies whole pages
byte-for-byte -- no struct parsing -- so the recording can drive struct
development and regression tests entirely offline. Kenneth runs this once during
a short session (~90 s, a few laps plus one deliberate off-track); the resulting
file is everything the next batch needs.

Run it directly::

    python -m pitwall.games.acc.capture --out acc-spa.pwcap --duration 90

or, once installed, via the ``pitwall-capture`` console script.

The capture loop takes injectable ``pages``/``clock``/``sleep`` so it can be
exercised in tests without ACC running or any real waiting.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pitwall import timeutil
from pitwall.games.acc.recording import RecordingWriter
from pitwall.games.acc.shm import GameNotRunningError, LivePages


class Pages(Protocol):
    """The page-provider surface :func:`capture_session` needs (live or fake)."""

    def sizes(self) -> dict[str, int]: ...
    def static_bytes(self) -> bytes: ...
    def physics_bytes(self) -> bytes: ...
    def graphics_bytes(self) -> bytes: ...
    def close(self) -> None: ...


@dataclass
class CaptureStats:
    """What a capture produced."""

    path: Path
    physics_samples: int
    graphics_samples: int
    elapsed_s: float
    page_sizes: dict[str, int]


def capture_session(
    out_path: str | Path,
    *,
    duration_s: float = 90.0,
    poll_hz: float = 100.0,
    pages: Pages | None = None,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = print,
) -> CaptureStats:
    """Record ``duration_s`` of ACC pages to ``out_path`` at ~``poll_hz``.

    Captures the static page once, then physics+graphics each tick. Opening
    :class:`LivePages` raises :class:`GameNotRunningError` if ACC isn't running;
    the loop body is otherwise game-agnostic.
    """
    owns_pages = pages is None
    if pages is None:
        pages = LivePages()
    period = 1.0 / poll_hz
    started_at = timeutil.to_iso_utc(timeutil.utc_now())

    def report(msg: str) -> None:
        if progress is not None:
            progress(msg)

    try:
        writer = RecordingWriter(
            out_path,
            page_sizes=pages.sizes(),
            poll_hz=poll_hz,
            started_at_utc=started_at,
            note="raw ACC pages; structs validated offline (C3)",
        )
        with writer:
            start = clock()
            writer.write_static(0, pages.static_bytes())
            next_report = 1.0
            elapsed = 0.0
            while True:
                elapsed = clock() - start
                if elapsed >= duration_s:
                    break
                ts = int(elapsed * 1_000_000)
                writer.write_physics(ts, pages.physics_bytes())
                writer.write_graphics(ts, pages.graphics_bytes())
                if elapsed >= next_report:
                    report(f"  {elapsed:5.1f}s / {duration_s:.0f}s  -  {writer.counts['physics']} physics frames")
                    next_report += 1.0
                sleep(period)
    finally:
        if owns_pages:
            pages.close()

    stats = CaptureStats(
        path=Path(out_path),
        physics_samples=writer.counts["physics"],
        graphics_samples=writer.counts["graphics"],
        elapsed_s=elapsed,
        page_sizes=writer.meta["page_sizes"],
    )
    report(
        f"wrote {stats.path}  ({stats.physics_samples} physics + {stats.graphics_samples} graphics "
        f"frames over {stats.elapsed_s:.1f}s, pages {stats.page_sizes})"
    )
    return stats


def _default_out() -> str:
    return "acc-" + timeutil.utc_now().strftime("%Y%m%d-%H%M%S") + ".pwcap"


def main(argv: list[str] | None = None) -> int:
    """Console entry point for ``pitwall-capture`` / ``python -m ...capture``."""
    parser = argparse.ArgumentParser(
        prog="pitwall-capture",
        description="Capture ACC's raw shared-memory pages to a .pwcap recording.",
    )
    parser.add_argument("--out", default=None, help="output .pwcap path (default: acc-<timestamp>.pwcap)")
    parser.add_argument("--duration", type=float, default=90.0, help="seconds to record (default: 90)")
    parser.add_argument("--hz", type=float, default=100.0, help="poll rate in Hz (default: 100)")
    args = parser.parse_args(argv)

    out = args.out or _default_out()
    print(f"Capturing ACC shared memory -> {out}  ({args.duration:.0f}s @ {args.hz:.0f} Hz). Drive now.")
    try:
        capture_session(out, duration_s=args.duration, poll_hz=args.hz)
    except GameNotRunningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
