"""The unit of telemetry flowing through ingest.

A :class:`CanonicalFrame` is one sample, already mapped to canonical channel
names, plus the metadata the pipeline needs to cut the stream into laps and
sectors. :class:`StaticInfo` is the once-per-session context (track, car, length).
Game readers (synthetic now, ACC in C3) emit these; nothing downstream knows or
cares which game produced them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StaticInfo:
    """Once-per-session context from a game's static page."""

    game: str
    track_code: str
    car_code: str
    started_at_utc: str
    sector_count: int = 3
    track_length_m: float | None = None
    session_type: str | None = None
    air_temp_c: float | None = None
    road_temp_c: float | None = None


@dataclass
class CanonicalFrame:
    """One telemetry sample.

    ``values`` holds canonical channel name -> value and must include
    ``lap_distance_m``. The remaining fields drive lap/sector segmentation and
    are not stored as trace channels.
    """

    t: float  # seconds on the source clock, monotonically increasing
    values: dict[str, float] = field(default_factory=dict)
    lap_count: int = 0  # completed laps so far; increments at the start/finish line
    sector_index: int = 0  # 0-based current sector
    lap_valid: bool = True  # current lap still valid (not invalidated off-track)
    in_pit: bool = False
    tyre_set: int = 0
