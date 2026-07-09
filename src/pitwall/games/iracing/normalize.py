"""Lap detection from the raw iRacing telemetry stream.

PORT of the visualiser's ``rtv/ingest/normalize.py`` -- kept near-verbatim so iRacing laps
are detected by the identical logic in both projects. The only divergences: the ``Lap``
dataclass is inlined here (pitwall does not need the visualiser's wider domain model), and
the unused ``rtv`` logger is dropped.

pitwall consumes this only to derive a per-frame *current lap number* (the visualiser also
keeps the completed-``Lap`` records); the adapter projects that onto
``CanonicalFrame.lap_count`` so pitwall's own ``segment()`` cuts laps at the same points.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Lap:
    """A single lap marker derived from the telemetry stream."""

    session_id: str
    lap: int
    start_tick: int
    end_tick: int | None = None
    start_session_time: float = 0.0
    lap_time: float | None = None
    is_valid: bool = True
    is_out_lap: bool = False
    is_in_lap: bool = False


class LapTracker:
    """Derives :class:`Lap` markers from successive telemetry ticks.

    Primary signal is the ``Lap`` channel (most reliable). We also watch
    ``LapDistPct`` for the start/finish wrap as a cross-check, and flag out-laps
    (lap that begins on pit road) and in-laps (lap that ends entering pits).
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.laps: list[Lap] = []
        self._current: Lap | None = None
        self._last_lap_num: int | None = None
        self._last_dist_pct: float | None = None

    def update(self, values: dict[str, Any], *, tick: int, session_time: float) -> Lap | None:
        """Process one tick. Returns a just-completed Lap, if any."""
        lap_num = _as_int(values.get("Lap"))
        dist_pct = _as_float(values.get("LapDistPct"))
        on_pit = bool(values.get("OnPitRoad") or False)
        completed: Lap | None = None

        if lap_num is None:
            return None

        if self._current is None:
            # First tick we see -- open a lap.
            self._current = Lap(
                session_id=self.session_id,
                lap=lap_num,
                start_tick=tick,
                start_session_time=session_time,
                is_out_lap=on_pit,
            )
            self._last_lap_num = lap_num
        elif lap_num != self._last_lap_num and lap_num > (self._last_lap_num or -1):
            # Lap counter advanced -> close the current lap, open the next.
            self._current.end_tick = tick
            last_lap_time = _as_float(values.get("LapLastLapTime"))
            if last_lap_time and last_lap_time > 0:
                self._current.lap_time = last_lap_time
            else:
                self._current.lap_time = session_time - self._current.start_session_time
            self._current.is_in_lap = on_pit
            self.laps.append(self._current)
            completed = self._current

            self._current = Lap(
                session_id=self.session_id,
                lap=lap_num,
                start_tick=tick,
                start_session_time=session_time,
                is_out_lap=on_pit,
            )
            self._last_lap_num = lap_num

        self._last_dist_pct = dist_pct
        return completed

    def finalize(self, *, tick: int, session_time: float) -> None:
        """Close the in-progress lap at end of stream/import."""
        if self._current is not None and self._current.end_tick is None:
            self._current.end_tick = tick
            self._current.lap_time = session_time - self._current.start_session_time
            self.laps.append(self._current)
            self._current = None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
