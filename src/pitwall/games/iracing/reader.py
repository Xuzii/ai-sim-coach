"""The iRacing :class:`~pitwall.ingest.source.TelemetrySource` -- the canonical adapter.

Glues the ported decode layer (:mod:`ibt` / :mod:`normalize` / :mod:`ibt_types`) to
pitwall's pipeline: it streams ``.ibt`` samples, runs each through the shared
:class:`~pitwall.games.iracing.normalize.LapTracker` (so laps are detected exactly as the
visualiser does), and emits canonical frames via :mod:`mapping`. The constructor takes an
already-opened ``IBT``-like object so tests inject a fake without ``pyirsdk`` -- mirroring
how the ACC reader takes an injectable frame stream.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pitwall import timeutil
from pitwall.games.iracing import ibt as ibt_mod
from pitwall.games.iracing import mapping
from pitwall.games.iracing.normalize import LapTracker
from pitwall.ingest.frames import CanonicalFrame, StaticInfo
from pitwall.ingest.source import TelemetrySource

# "...rudskogen 2026-06-04 21-44-32.ibt" -- iRacing stamps the export filename in local time.
_FILENAME_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})")


class IRacingSource(TelemetrySource):
    """Replays an iRacing ``.ibt`` file as canonical frames."""

    native_hz = 60.0

    def __init__(self, ibt: Any, info: dict[str, Any], *, started_at_utc: str) -> None:
        self._ibt = ibt
        self._info = info
        self._started_at_utc = started_at_utc
        self._starts = mapping.sector_starts_pct(info)

    @classmethod
    def from_ibt_file(cls, path: str | Path, *, started_at_utc: str | None = None) -> IRacingSource:
        """Open a ``.ibt`` file (needs the ``iracing`` extra) and build a source for it."""
        ibt = ibt_mod.open_ibt(path)
        info = ibt_mod.read_ibt_session_info(ibt)
        started = started_at_utc or _started_at_from_path(path)
        return cls(ibt, info, started_at_utc=started)

    def static_info(self) -> StaticInfo:
        return mapping.to_static_info(self._info, started_at_utc=self._started_at_utc)

    def frames(self) -> Iterator[CanonicalFrame]:
        tracker = LapTracker("ibt")
        try:
            for i, values in enumerate(ibt_mod.iter_samples(self._ibt, mapping.IBT_VARS)):
                st = values.get("SessionTime")
                t = float(st) if st is not None else float(i)
                tick = int(values.get("SessionTick") or i)
                tracker.update(values, tick=tick, session_time=t)
                lap_count = tracker._current.lap if tracker._current is not None else 0
                yield mapping.to_frame(values, t=t, lap_count=lap_count, starts=self._starts)
        finally:
            close = getattr(self._ibt, "close", None)
            if callable(close):
                close()


def _started_at_from_path(path: str | Path) -> str:
    """Session start (UTC ISO) from the ``.ibt`` filename timestamp, else file mtime."""
    p = Path(path)
    m = _FILENAME_TS.search(p.name)
    if m:
        y, mo, d, hh, mm, ss = (int(g) for g in m.groups())
        try:
            local = datetime(y, mo, d, hh, mm, ss)  # naive == system local time
            return timeutil.to_iso_utc(local.astimezone(timezone.utc))
        except ValueError:
            pass
    try:
        return timeutil.to_iso_utc(datetime.fromtimestamp(p.stat().st_mtime, timezone.utc))
    except OSError:
        return timeutil.to_iso_utc(timeutil.utc_now())
