"""The ingest spine: downsample -> segment into laps/sectors -> persist.

Game-agnostic. Takes any :class:`~pitwall.ingest.source.TelemetrySource`,
decimates the stream to a target rate (ACC's 333 Hz -> 50 Hz), cuts it into laps
at start/finish-line crossings and sectors at sector-index changes, then writes
one SQLite row + one Parquet trace per lap. Stints advance when the tyre set
changes between laps.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from pitwall import channels
from pitwall.ingest.frames import CanonicalFrame, StaticInfo
from pitwall.ingest.source import TelemetrySource
from pitwall.naming import car_name, track_name
from pitwall.storage import db, traces


@dataclass
class LapBuffer:
    """Frames belonging to one lap, with the times of its start/finish line crossings."""

    frames: list[CanonicalFrame] = field(default_factory=list)
    start_t: float = 0.0
    end_t: float = 0.0
    lap_count: int = 0  # completedLaps value *during* this lap; lap_number = lap_count + 1
    partial: bool = False  # closed at end-of-stream rather than at a line crossing


def downsample(frames: Iterable[CanonicalFrame], target_hz: float) -> Iterator[CanonicalFrame]:
    """Decimate to ~target_hz, but always keep frames where the lap or sector
    changes so segmentation stays exact."""
    target_dt = 1.0 / target_hz
    last_kept_t: float | None = None
    prev_lap: int | None = None
    prev_sector: int | None = None
    for f in frames:
        boundary = prev_lap is not None and (f.lap_count != prev_lap or f.sector_index != prev_sector)
        if last_kept_t is None or boundary or (f.t - last_kept_t) >= target_dt - 1e-9:
            yield f
            last_kept_t = f.t
        prev_lap = f.lap_count
        prev_sector = f.sector_index


def segment(frames: Iterable[CanonicalFrame]) -> Iterator[LapBuffer]:
    """Group frames into laps at start/finish-line crossings (``lap_count`` increments)."""
    buf: list[CanonicalFrame] = []
    start_t = 0.0
    prev_lap: int | None = None
    for f in frames:
        if prev_lap is not None and f.lap_count != prev_lap:
            yield LapBuffer(frames=buf, start_t=start_t, end_t=f.t, lap_count=prev_lap)
            buf = []
        if not buf:
            start_t = f.t
        buf.append(f)
        prev_lap = f.lap_count
    if buf:
        yield LapBuffer(frames=buf, start_t=start_t, end_t=buf[-1].t, lap_count=prev_lap or 0, partial=True)


def _sector_breakdown(
    buf: LapBuffer, sector_count: int
) -> tuple[list[int | None], list[float | None], list[float | None]]:
    """Per-sector times (ms) and distance bounds for one lap.

    Partitions the *exact* ``[start_t, end_t]`` interval that defines ``lap_time``
    using forward-only sector progression, so a clean complete lap's sector times
    telescope to ``lap_time_ms`` by construction:

    - Sector 0 always opens at the lap's first frame / ``start_t`` (the start/finish
      crossing), regardless of what ``sector_index`` reads on the leading frames.
      ACC's ``currentSectorIndex`` can lag the lap-counter increment by a few frames,
      so a lap can begin still reporting the *previous* lap's last sector; ignoring
      the stale value is what stops that sliver from going uncounted (the old code
      mis-attributed it, so sectors summed short of the lap time).
    - We open the next sector only when ``sector_index`` steps to *exactly* the next
      index (``cur + 1``). A leading frame reading a higher index (a stale ``2`` at
      the start) and a premature reset toward 0 near the end both fail that test, so
      the frame stays in the current sector -- forward progression only.
    - The last open sector ends at ``buf.end_t`` (the next lap's first-frame time),
      not the last frame's ``t``, so the partition closes exactly on ``lap_time``.
      Its distance upper bound is the last frame's ``lap_distance_m`` (the next lap's
      distance has already wrapped toward 0 and is unusable).
    - Sectors whose boundary is never reached (out/in/pit/incomplete laps that don't
      progress 0->1->2) stay None; we never fabricate a partition.
    """
    times: list[int | None] = [None] * sector_count
    starts: list[float | None] = [None] * sector_count
    ends: list[float | None] = [None] * sector_count
    if not buf.frames:
        return times, starts, ends

    def dist(f: CanonicalFrame) -> float:
        return float(f.values["lap_distance_m"])

    # Sector 0 always opens at the lap boundary; ignore any stale leading index.
    cur = 0
    seg_start_t = buf.start_t
    starts[0] = dist(buf.frames[0])
    for f in buf.frames:
        # Open the next sector only on an exact forward step to cur+1. A stale
        # leading index (> cur+1) or a premature reset (< cur+1) is ignored, so the
        # frame stays attributed to the current sector.
        if cur + 1 < sector_count and f.sector_index == cur + 1:
            times[cur] = int(round((f.t - seg_start_t) * 1000))
            ends[cur] = dist(f)
            seg_start_t = f.t
            cur += 1
            starts[cur] = dist(f)
    # Close the final open sector on the lap's wall-clock end so the times
    # telescope exactly to (end_t - start_t) == lap_time_ms.
    times[cur] = int(round((buf.end_t - seg_start_t) * 1000))
    ends[cur] = dist(buf.frames[-1])
    return times, starts, ends


def _trace_data(buf: LapBuffer) -> dict[str, list]:
    """Build the column->values mapping for a lap's Parquet trace."""
    data: dict[str, list] = {
        ch: [f.values.get(ch) for f in buf.frames] for ch in channels.CHANNEL_NAMES
    }
    data[channels.TIME_KEY] = [int(round((f.t - buf.start_t) * 1000)) for f in buf.frames]
    return data


def _persist_lap(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    lap_number: int,
    stint_number: int,
    buf: LapBuffer,
    session_started_utc: str,
) -> int:
    speeds = [f.values["speed_kmh"] for f in buf.frames]
    fuel0 = buf.frames[0].values.get("fuel_kg")
    fuel1 = buf.frames[-1].values.get("fuel_kg")
    fuel_used = max(0.0, fuel0 - fuel1) if (fuel0 is not None and fuel1 is not None) else None

    rel = traces.lap_trace_relpath(session_id, lap_number)
    abs_path = traces.resolve_trace_path(rel)
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    point_count = traces.write_lap_trace(abs_path, _trace_data(buf))

    return db.insert_lap(
        conn,
        session_id=session_id,
        lap_number=lap_number,
        started_at_utc=session_started_utc,
        trace_path=rel,
        point_count=point_count,
        stint_number=stint_number,
        lap_time_ms=int(round((buf.end_t - buf.start_t) * 1000)),
        is_valid=all(f.lap_valid for f in buf.frames),
        is_outlap=buf.frames[0].in_pit,
        is_inlap=buf.frames[-1].in_pit,
        top_speed_kmh=max(speeds),
        avg_speed_kmh=sum(speeds) / len(speeds),
        fuel_used_kg=fuel_used,
        tyre_set=buf.frames[0].tyre_set,
    )


def run(
    source: TelemetrySource,
    conn: sqlite3.Connection,
    *,
    target_hz: float = 50.0,
    min_points: int = 20,
    on_lap: Callable[[int, int | None], None] | None = None,
) -> int:
    """Ingest a whole session from ``source`` into ``conn``. Returns the session id.

    ``on_lap`` (optional) is invoked once per committed lap with
    ``(lap_number, lap_time_ms)`` -- purely a progress hook for callers like the
    ``pitwall-ingest`` CLI. It defaults to None and changes nothing about ingestion.
    """
    info: StaticInfo = source.static_info()
    session_id = db.insert_session(
        conn,
        game=info.game,
        track_code=info.track_code,
        track_name=track_name(info.track_code),
        car_code=info.car_code,
        car_name=car_name(info.car_code),
        started_at_utc=info.started_at_utc,
        session_type=info.session_type,
        sector_count=info.sector_count,
        track_length_m=info.track_length_m,
        air_temp_c=info.air_temp_c,
        road_temp_c=info.road_temp_c,
    )

    stint_number = 1
    prev_tyre: int | None = None
    for buf in segment(downsample(source.frames(), target_hz)):
        if len(buf.frames) < min_points:
            continue  # discard the trailing one-frame buffer after the final line crossing
        tyre_set = buf.frames[0].tyre_set
        if prev_tyre is not None and tyre_set != prev_tyre:
            stint_number += 1
        prev_tyre = tyre_set
        lap_id = _persist_lap(
            conn,
            session_id=session_id,
            lap_number=buf.lap_count + 1,
            stint_number=stint_number,
            buf=buf,
            session_started_utc=info.started_at_utc,
        )
        times, starts, ends = _sector_breakdown(buf, info.sector_count)
        db.insert_sectors(conn, lap_id, times, start_distances=starts, end_distances=ends)
        if on_lap is not None:
            on_lap(buf.lap_count + 1, int(round((buf.end_t - buf.start_t) * 1000)))

    return session_id
