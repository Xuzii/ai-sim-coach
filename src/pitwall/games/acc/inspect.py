"""Human-readable dump of a ``.pwcap`` recording, for eyeballing struct decode.

This is the "confirm correctness before trusting it" tool: it parses the static
page and a handful of physics/graphics frames and prints the decoded values plus
session-wide aggregates (lap progression, sector set, channel ranges, status
histogram, packetId monotonicity, effective rate). If these look sane, the struct
layout is right. Run via the ``pitwall-inspect`` console script.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from pitwall import naming
from pitwall.games.acc.recording import PageKind, Recording, read_recording
from pitwall.games.acc.structs import AcStatus, parse_graphics, parse_physics, parse_static, wstr


def _ascii(s: str) -> str:
    """Make a decoded string safe to print on a non-UTF-8 console."""
    return s.encode("ascii", "backslashreplace").decode("ascii")


def _fmt(values) -> str:
    return "[" + ", ".join(f"{v:.2f}" for v in values) + "]"


def dump(recording: Recording, *, sample_frames: int = 5) -> str:
    """Render a recording as a multi-line report string."""
    out: list[str] = []
    meta = recording.meta
    out.append(f"format v{meta.get('version')}  game={meta.get('game')}  "
               f"poll_hz={meta.get('poll_hz')}  scrubbed={meta.get('scrubbed')}")
    if meta.get("note"):
        out.append(f"note: {meta['note']}")

    static_rec = recording.static()
    if static_rec is not None:
        st = parse_static(static_rec.data)
        track = wstr(st.track)
        car = wstr(st.carModel)
        out.append("")
        out.append("STATIC")
        out.append(f"  track   = {_ascii(track)}  ({naming.track_name(track)})")
        out.append(f"  car     = {_ascii(car)}  ({naming.car_name(car)})")
        out.append(f"  player  = {_ascii(wstr(st.playerName))!r} {_ascii(wstr(st.playerSurname))!r} "
                   f"nick={_ascii(wstr(st.playerNick))!r}")
        out.append(f"  sm/ac   = {_ascii(wstr(st.smVersion))} / {_ascii(wstr(st.acVersion))}")
        out.append(f"  sectors={st.sectorCount}  maxRpm={st.maxRpm}  "
                   f"trackSPlineLength={st.trackSPlineLength:.1f}")

    phys = recording.of_kind(PageKind.PHYSICS)
    graph = recording.of_kind(PageKind.GRAPHICS)
    out.append("")
    out.append(f"FRAMES: {len(phys)} physics / {len(graph)} graphics")
    if not phys:
        return "\n".join(out)

    span_s = (phys[-1].ts_micros - phys[0].ts_micros) / 1_000_000.0
    eff_hz = (len(phys) - 1) / span_s if span_s > 0 else 0.0
    out.append(f"  span={span_s:.1f}s  effective_rate={eff_hz:.1f} Hz")

    n = len(phys)
    idxs = sorted({int(p * (n - 1)) for p in (i / max(1, sample_frames - 1) for i in range(sample_frames))})
    out.append("")
    out.append("SAMPLES (t  gas brk clu  gear rpm  speed   accG          | lap sec normPos valid pit tyre)")
    for i in idxs:
        p = parse_physics(phys[i].data)
        gi = min(i, len(graph) - 1)
        g = parse_graphics(graph[gi].data)
        out.append(
            f"  {phys[i].ts_micros / 1e6:6.1f}  {p.gas:.2f} {p.brake:.2f} {p.clutch:.2f}  "
            f"{p.gear - 1:>2d} {p.rpms:5d}  {p.speedKmh:6.1f}  {_fmt(p.accG)}"
            f" | {g.completedLaps:>3d} {g.currentSectorIndex:>3d} {g.normalizedCarPosition:7.4f} "
            f"{g.isValidLap:>3d} {g.isInPitLane:>3d} {g.currentTyreSet:>3d}"
        )

    # Aggregates
    laps: list[int] = []
    prev = None
    status_hist: Counter[int] = Counter()
    sector_set = set()
    for r in graph:
        g = parse_graphics(r.data)
        status_hist[int(g.status)] += 1
        sector_set.add(int(g.currentSectorIndex))
        if g.completedLaps != prev:
            laps.append(int(g.completedLaps))
            prev = g.completedLaps

    pids = [parse_physics(r.data).packetId for r in phys]
    speeds = [parse_physics(r.data).speedKmh for r in phys[:: max(1, n // 500)]]
    out.append("")
    out.append("AGGREGATES")
    out.append(f"  completedLaps progression = {laps}")
    out.append(f"  sector index set          = {sorted(sector_set)}")
    out.append(f"  status histogram          = "
               f"{{{', '.join(f'{AcStatus(k).name}={v}' for k, v in sorted(status_hist.items()))}}}")
    monotonic = all(b >= a for a, b in zip(pids, pids[1:], strict=False))
    out.append(f"  packetId monotonic        = {monotonic}  ({pids[0]}..{pids[-1]})")
    out.append(f"  speed range (km/h)        = {min(speeds):.1f} .. {max(speeds):.1f}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """Console entry point for ``pitwall-inspect``."""
    parser = argparse.ArgumentParser(
        prog="pitwall-inspect",
        description="Print a human-readable decode of a .pwcap recording.",
    )
    parser.add_argument("path", help="the .pwcap recording to inspect")
    parser.add_argument("--frames", type=int, default=5, help="number of sample frames to print")
    args = parser.parse_args(argv)

    if not Path(args.path).exists():
        print(f"error: no such file: {args.path}", file=sys.stderr)
        return 2
    print(dump(read_recording(args.path), sample_frames=args.frames))
    return 0


if __name__ == "__main__":
    sys.exit(main())
