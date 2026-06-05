"""Deterministic consistency score -- computed in Python, never hallucinated.

A consistent driver puts in laps (and sectors) of nearly the same time; an
inconsistent one is all over the place. We quantify that as a 0-100 score from the
*spread* of the session's valid, timed laps, so the coach can ground any remark
about repeatability in a real number (exposed to the agent as the ``get_consistency``
tool).

Method (documented so it is reproducible and tunable as real data arrives):

1. Keep only **valid, timed, non-out/in** laps -- out/in laps distort the spread.
   Need at least two such laps, else the score is ``None``.
2. Compute the **coefficient of variation** (CoV = stdev / mean, dimensionless) of
   the lap times, and the mean CoV across sectors (each sector's times across the
   laps). Sectors with fewer than two timed samples are skipped.
3. Combine: ``cov = 0.5 * lap_time_cov + 0.5 * mean_sector_cov`` (lap-time only if no
   sector data), then map linearly to a score with a cap of :data:`COV_CAP` (10%):
   ``score = 100 * (1 - min(1, cov / 0.10))``. So 0% spread -> 100, 1% -> 90,
   5% -> 50, >=10% -> 0.

The 10% cap is a starting heuristic; the components are returned alongside the score
so the mapping can be recalibrated against real captures without changing callers.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover -- type hints only, no runtime coupling
    from collections.abc import Mapping, Sequence

    from pitwall.storage.db import Lap, Sector

# Weights for combining lap-time and sector spread, and the CoV that maps to score 0.
LAP_TIME_WEIGHT = 0.5
SECTOR_WEIGHT = 0.5
COV_CAP = 0.10


def consistency_score(
    laps: Sequence[Lap],
    sectors_by_lap: Mapping[int, Sequence[Sector]],
) -> dict:
    """Return a consistency report for a set of laps.

    ``laps`` is the candidate laps (the function filters to valid/timed/non-out-in);
    ``sectors_by_lap`` maps ``lap_id`` to that lap's sectors. The result dict has::

        {"score": int|None, "lap_count": int, "basis": str,
         "lap_time_cov": float|None, "mean_sector_cov": float|None}
    """
    usable = [
        lap for lap in laps
        if lap.is_valid and lap.lap_time_ms and not lap.is_outlap and not lap.is_inlap
    ]
    n = len(usable)
    if n < 2:
        return {
            "score": None,
            "lap_count": n,
            "basis": f"need at least 2 valid, timed laps to measure consistency (have {n})",
            "lap_time_cov": None,
            "mean_sector_cov": None,
        }

    times = [float(lap.lap_time_ms) for lap in usable]
    lap_time_cov = _cov(times)

    sector_times: dict[int, list[float]] = defaultdict(list)
    for lap in usable:
        for sec in sectors_by_lap.get(lap.lap_id, []):
            if sec.sector_time_ms:
                sector_times[sec.sector_index].append(float(sec.sector_time_ms))
    per_sector_covs = [_cov(v) for v in sector_times.values() if len(v) >= 2]
    mean_sector_cov = statistics.fmean(per_sector_covs) if per_sector_covs else None

    if mean_sector_cov is not None:
        combined = LAP_TIME_WEIGHT * lap_time_cov + SECTOR_WEIGHT * mean_sector_cov
    else:
        combined = lap_time_cov
    score = round(100.0 * (1.0 - min(1.0, combined / COV_CAP)))

    msc_txt = f"{mean_sector_cov:.4f}" if mean_sector_cov is not None else "n/a"
    return {
        "score": score,
        "lap_count": n,
        "basis": (
            f"{n} valid timed laps; lap-time CoV {lap_time_cov:.4f}, mean sector CoV {msc_txt}; "
            "higher score = tighter spread (0-100)"
        ),
        "lap_time_cov": round(lap_time_cov, 4),
        "mean_sector_cov": round(mean_sector_cov, 4) if mean_sector_cov is not None else None,
    }


def _cov(values: list[float]) -> float:
    """Coefficient of variation (sample stdev / mean). Requires len >= 2."""
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0
    return statistics.stdev(values) / mean
