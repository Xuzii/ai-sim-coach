"""Shaping telemetry into token-cheap, coach-friendly responses.

Two strategies, both keeping Claude's context window safe:

* **summary** -- min/max/mean/std per channel, per sector and whole-lap. A raw
  50 Hz trace is tens of thousands of tokens; this is the default.
* **distance bins** -- the trace aggregated into a fixed number of equal-distance
  bins (mean per bin). Distance, not time, because a coach reasons about *places
  on the track*. The bin count per detail level caps the token cost regardless of
  lap length; a ``distance_range`` zoom puts all those bins in the chosen window.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from pitwall import channels
from pitwall.storage.db import Sector

#: Channels returned when a trace query does not name any -- the ones a coach
#: reaches for first.
DEFAULT_TRACE_CHANNELS: tuple[str, ...] = (
    "throttle", "brake", "speed_kmh", "steering_angle", "rpm", "gear",
)

#: Distance-bin counts per detail level. summary returns stats, not a series.
DETAIL_BINS: dict[str, int] = {"low": 60, "medium": 150, "full": 600}
DETAIL_LEVELS: tuple[str, ...] = ("summary", "low", "medium", "full")


def _round(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(float(x), nd)


def channel_summary(table: pa.Table, channel: str) -> dict:
    """min/max/mean/std for one channel over the given (already-sliced) table."""
    col = table.column(channel)
    if col.length() == 0 or col.null_count == col.length():
        return {"min": None, "max": None, "mean": None, "std": None}
    mm = pc.min_max(col).as_py()
    return {
        "min": _round(mm["min"]),
        "max": _round(mm["max"]),
        "mean": _round(pc.mean(col).as_py()),
        "std": _round(pc.stddev(col, ddof=0).as_py()),
    }


def slice_distance(table: pa.Table, lo: float | None, hi: float | None) -> pa.Table:
    if lo is None or hi is None:
        return table
    dist = table.column(channels.DISTANCE_KEY)
    mask = pc.and_(pc.greater_equal(dist, lo), pc.less(dist, hi))
    return table.filter(mask)


def summary_by_sector(table: pa.Table, sectors: Sequence[Sector], names: Sequence[str]) -> dict:
    """Per-sector channel summaries, keyed by 1-based sector number."""
    out: dict[int, dict] = {}
    for s in sectors:
        sub = slice_distance(table, s.start_distance_m, s.end_distance_m)
        out[s.sector_index + 1] = {name: channel_summary(sub, name) for name in names}
    return out


def n_bins_for(detail_level: str) -> int:
    return DETAIL_BINS.get(detail_level, DETAIL_BINS["low"])


def bin_by_distance(table: pa.Table, names: Sequence[str], n_bins: int) -> list[dict]:
    """Aggregate channels into ``n_bins`` equal-distance bins (mean per bin).

    Empty bins are dropped. Each point carries the bin-centre distance and the
    mean of each requested channel.
    """
    dist = table.column(channels.DISTANCE_KEY).to_numpy(zero_copy_only=False).astype(float)
    if dist.size == 0:
        return []
    lo, hi = float(dist.min()), float(dist.max())
    if hi <= lo:
        n_bins = 1
        edges = np.array([lo, lo + 1.0])
    else:
        edges = np.linspace(lo, hi, n_bins + 1)
    idx = np.clip(np.digitize(dist, edges[1:-1]), 0, n_bins - 1)
    counts = np.bincount(idx, minlength=n_bins).astype(float)
    centres = (edges[:-1] + edges[1:]) / 2.0

    means: dict[str, np.ndarray] = {}
    for name in names:
        vals = table.column(name).to_numpy(zero_copy_only=False).astype(float)
        sums = np.bincount(idx, weights=vals, minlength=n_bins)
        with np.errstate(invalid="ignore", divide="ignore"):
            means[name] = sums / counts

    points: list[dict] = []
    for b in range(n_bins):
        if counts[b] == 0:
            continue
        point = {"distance_m": round(float(centres[b]), 1)}
        for name in names:
            v = means[name][b]
            point[name] = None if np.isnan(v) else round(float(v), 3)
        points.append(point)
    return points
