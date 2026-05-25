"""Turn a raw ``.pwcap`` capture into a small, committable CI fixture.

A raw recording is huge (4 KiB pages x ~100 Hz x minutes) and contains the
player's name on the static page. :func:`slim_recording` makes a fixture that is
safe and tiny enough to commit:

* **scrub** the player identity fields off the static page (we now know the
  offsets, which we didn't at capture time);
* **truncate** each page payload to its true struct size (4096 -> 800/1588/820),
  the single biggest size win -- the reader slices to ``sizeof`` anyway;
* **decimate** to roughly a quarter rate and optionally **window** to a short
  span, so the fixture stays under a couple of MB while still covering a real
  lap-line crossing.

Run via the ``pitwall-scrub`` console script.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from dataclasses import dataclass
from pathlib import Path

from pitwall.games.acc import structs
from pitwall.games.acc.recording import PageKind, RecordingWriter, read_recording

_STRUCT_SIZE = {
    PageKind.STATIC: ctypes.sizeof(structs.SPageFileStatic),
    PageKind.PHYSICS: ctypes.sizeof(structs.SPageFilePhysics),
    PageKind.GRAPHICS: ctypes.sizeof(structs.SPageFileGraphic),
}
_KIND_KEY = {PageKind.STATIC: "static", PageKind.PHYSICS: "physics", PageKind.GRAPHICS: "graphics"}


def scrub_static(static_bytes: bytes) -> bytes:
    """Zero the player name/surname/nick fields on a static-page snapshot."""
    buf = bytearray(static_bytes)
    for field in structs.PLAYER_IDENTITY_FIELDS:
        f = getattr(structs.SPageFileStatic, field)
        if f.offset + f.size <= len(buf):
            buf[f.offset : f.offset + f.size] = b"\x00" * f.size
    return bytes(buf)


@dataclass
class SlimStats:
    path: Path
    physics: int
    graphics: int
    bytes_out: int


def slim_recording(
    src: str | Path,
    dst: str | Path,
    *,
    window_s: tuple[float, float] | None = None,
    keep_every: int = 1,
    truncate_to_struct: bool = True,
    note: str = "",
) -> SlimStats:
    """Write a scrubbed, decimated, optionally windowed copy of ``src`` to ``dst``.

    ``window_s`` keeps only physics/graphics records whose timestamp falls in the
    inclusive ``(start_s, end_s)`` span; ``keep_every`` keeps every Nth record of
    each kind. Timestamps are rebased so the fixture starts at 0.
    """
    rec = read_recording(src)

    def truncate(kind: PageKind, data: bytes) -> bytes:
        return data[: _STRUCT_SIZE[kind]] if truncate_to_struct else data

    def in_window(ts_micros: int) -> bool:
        if window_s is None:
            return True
        return window_s[0] <= ts_micros / 1_000_000.0 <= window_s[1]

    phys = [r for r in rec.of_kind(PageKind.PHYSICS) if in_window(r.ts_micros)][::keep_every]
    graph = [r for r in rec.of_kind(PageKind.GRAPHICS) if in_window(r.ts_micros)][::keep_every]
    if not phys:
        raise ValueError("window/decimation kept no physics frames")

    base_ts = min(phys[0].ts_micros, graph[0].ts_micros if graph else phys[0].ts_micros)
    merged = sorted(phys + graph, key=lambda r: r.ts_micros)

    static = rec.static()
    if truncate_to_struct:
        page_sizes = {key: _STRUCT_SIZE[kind] for kind, key in _KIND_KEY.items()}
    else:
        page_sizes = dict(rec.meta["page_sizes"])

    writer = RecordingWriter(
        dst,
        page_sizes=page_sizes,
        poll_hz=rec.meta.get("poll_hz", 0.0) / max(1, keep_every),
        started_at_utc=rec.meta.get("started_at_utc", ""),
        game=rec.meta.get("game", "acc"),
        scrubbed=True,
        note=note or "scrubbed + slimmed CI fixture",
    )
    with writer:
        if static is not None:
            writer.write_static(0, truncate(PageKind.STATIC, scrub_static(static.data)))
        for r in merged:
            ts = max(0, r.ts_micros - base_ts)
            data = truncate(r.kind, r.data)
            if r.kind == PageKind.PHYSICS:
                writer.write_physics(ts, data)
            elif r.kind == PageKind.GRAPHICS:
                writer.write_graphics(ts, data)

    bytes_out = Path(dst).stat().st_size
    return SlimStats(Path(dst), writer.counts["physics"], writer.counts["graphics"], bytes_out)


def main(argv: list[str] | None = None) -> int:
    """Console entry point for ``pitwall-scrub``."""
    parser = argparse.ArgumentParser(
        prog="pitwall-scrub",
        description="Scrub + slim a raw .pwcap capture into a small CI fixture.",
    )
    parser.add_argument("--in", dest="src", required=True, help="raw input .pwcap")
    parser.add_argument("--out", dest="dst", required=True, help="output fixture .pwcap")
    parser.add_argument("--window", default=None, help="keep span START:END in seconds (e.g. 117:157)")
    parser.add_argument("--decimate", type=int, default=1, help="keep every Nth frame (default: 1)")
    parser.add_argument("--no-truncate", action="store_true", help="keep full page bytes")
    parser.add_argument("--note", default="", help="note recorded in the fixture metadata")
    args = parser.parse_args(argv)

    window = None
    if args.window:
        lo, hi = args.window.split(":")
        window = (float(lo), float(hi))

    stats = slim_recording(
        args.src, args.dst,
        window_s=window, keep_every=args.decimate,
        truncate_to_struct=not args.no_truncate, note=args.note,
    )
    print(
        f"wrote {stats.path}  ({stats.physics} physics + {stats.graphics} graphics frames, "
        f"{stats.bytes_out / 1_048_576:.2f} MiB, scrubbed)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
