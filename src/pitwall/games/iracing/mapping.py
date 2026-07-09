"""iRacing telemetry variables -> pitwall's canonical schema.

The pitwall-specific half of the iRacing reader (the other half, :mod:`ibt` /
:mod:`normalize` / :mod:`ibt_types`, is a faithful port of the visualiser's decode layer).
Pure functions over already-decoded ``{var: value}`` sample dicts and the parsed
session-info YAML -- no I/O -- so every conversion is a one-line unit test, exactly like
:mod:`pitwall.games.acc.mapping`. The field-by-field translation is documented in
``docs/channel-mapping.md``.

iRacing field names drift slightly between builds (``LFpressure`` vs ``LFpress``,
``LFtempCM`` vs ``LFtempCL``), so each canonical channel lists *candidate* source names and
we take the first one actually present in the file's headers (see :data:`IBT_VARS` and
``_first``).
"""

from __future__ import annotations

import re
from typing import Any

from pitwall import channels
from pitwall.ingest.frames import CanonicalFrame, StaticInfo

#: iRacing reports fuel in litres; race petrol is ~0.745 kg/L (same value the ACC mapping
#: uses). Kept local so the iRacing reader carries no dependency on the ACC package.
FUEL_DENSITY_KG_PER_L = 0.745
#: Standard gravity -- iRacing accelerations are m/s^2; canonical g_lat/g_lon are in g.
G = 9.80665
KPA_TO_PSI = 0.1450377
MS_TO_KMH = 3.6

#: iRacing corner code per canonical wheel (``channels.WHEEL_ORDER`` == fl, fr, rl, rr).
_CORNER = {"fl": "LF", "fr": "RF", "rl": "LR", "rr": "RR"}

#: Candidate iRacing var names per scalar canonical channel (first present wins).
_SCALAR_CANDIDATES: dict[str, tuple[str, ...]] = {
    "lap_distance_m": ("LapDist",),
    "throttle": ("Throttle",),
    "brake": ("Brake",),
    "clutch": ("Clutch",),
    "steering_angle": ("SteeringWheelAngle",),
    "gear": ("Gear",),
    "rpm": ("RPM",),
    "speed_kmh": ("Speed",),
    "g_lat": ("LatAccel",),
    "g_lon": ("LongAccel",),
    "fuel_kg": ("FuelLevel",),
}


def _corner_candidates(prefix: str, suffixes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix}{s}" for s in suffixes)


#: Per-corner tyre temp / pressure candidates (build-dependent suffixes).
_TEMP_SUFFIXES = ("tempCM", "tempCL", "tempM", "tempL")
_PRESS_SUFFIXES = ("pressure", "press")

#: Lap/sector/validity metadata channels the reader needs in addition to the sources above.
_META_VARS = ("SessionTime", "SessionTick", "Lap", "LapDistPct", "OnPitRoad", "LapLastLapTime")


def _all_source_vars() -> list[str]:
    names: list[str] = list(_META_VARS)
    for cands in _SCALAR_CANDIDATES.values():
        names.extend(cands)
    for corner in _CORNER.values():
        names.extend(_corner_candidates(corner, _TEMP_SUFFIXES))
        names.extend(_corner_candidates(corner, _PRESS_SUFFIXES))
    # De-dup, preserve order.
    return list(dict.fromkeys(names))


#: The full superset of var names to request from ``ibt.iter_samples``; absent ones are
#: skipped by the reader, so a build missing a channel simply yields None for it.
IBT_VARS: list[str] = _all_source_vars()


def _first(values: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    """First candidate present (not None) in ``values``, else None."""
    for name in candidates:
        v = values.get(name)
        if v is not None:
            return v
    return None


def _leading_number(s: Any) -> float | None:
    """Leading float in a quantity string like ``"3.25 km"`` / ``"25.55 C"``, else None."""
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(s))
    return float(m.group()) if m else None


def sector_starts_pct(info: dict[str, Any]) -> list[float]:
    """Sorted sector start fractions (0..1) from ``SplitTimeInfo.Sectors``.

    iRacing has no per-frame sector channel, so sectors are derived from ``LapDistPct``
    against these starts. Empty when the YAML lacks sector info (the caller then treats the
    lap as a single sector).
    """
    split = info.get("SplitTimeInfo") if isinstance(info, dict) else None
    sectors = split.get("Sectors") if isinstance(split, dict) else None
    if not isinstance(sectors, list):
        return []
    starts: list[float] = []
    for sec in sectors:
        if isinstance(sec, dict):
            pct = _leading_number(sec.get("SectorStartPct"))
            if pct is not None:
                starts.append(pct)
    return sorted(starts)


def sector_index_for(lap_dist_pct: float | None, starts: list[float]) -> int:
    """0-based sector for a lap fraction: how many sector starts it has passed.

    Forward-only progression (0,1,2,...) is exactly what
    ``pipeline._sector_breakdown`` expects. Falls back to sector 0 when there are no
    starts or the fraction is unknown.
    """
    if not starts or lap_dist_pct is None:
        return 0
    idx = 0
    for i, start in enumerate(starts):
        if lap_dist_pct >= start:
            idx = i
        else:
            break
    return idx


def _player_driver(info: dict[str, Any]) -> dict[str, Any]:
    driver_info = info.get("DriverInfo", {}) if isinstance(info, dict) else {}
    drivers = driver_info.get("Drivers")
    pcar_idx = driver_info.get("DriverCarIdx")
    if isinstance(drivers, list) and isinstance(pcar_idx, int):
        for d in drivers:
            if isinstance(d, dict) and d.get("CarIdx") == pcar_idx:
                return d
    return {}


def to_static_info(info: dict[str, Any], *, started_at_utc: str, game: str = "iracing") -> StaticInfo:
    """Build the once-per-session :class:`StaticInfo` from the parsed session-info YAML.

    Unlike ACC, iRacing supplies the track length, display names and sector layout directly
    in the YAML, so no lookup tables are needed -- the display names ride through
    ``StaticInfo.track_name`` / ``car_name`` (see the pipeline's name preference).
    """
    weekend = info.get("WeekendInfo", {}) if isinstance(info, dict) else {}
    track_code = str(weekend.get("TrackName") or "") or "unknown"
    track_display = weekend.get("TrackDisplayName") or weekend.get("TrackName")

    driver = _player_driver(info)
    car_code = str(driver.get("CarPath") or "") or "unknown"
    car_display = driver.get("CarScreenName") or driver.get("CarPath")

    length_km = _leading_number(weekend.get("TrackLength"))
    track_length_m = length_km * 1000.0 if length_km else None

    starts = sector_starts_pct(info)
    return StaticInfo(
        game=game,
        track_code=track_code,
        car_code=car_code,
        started_at_utc=started_at_utc,
        sector_count=len(starts) or 3,
        track_length_m=track_length_m,
        session_type=_session_type(info),
        air_temp_c=_leading_number(weekend.get("TrackAirTemp")),
        road_temp_c=_leading_number(weekend.get("TrackSurfaceTemp")),
        track_name=str(track_display) if track_display else None,
        car_name=str(car_display) if car_display else None,
    )


def _session_type(info: dict[str, Any]) -> str | None:
    """Best-effort session type (e.g. ``"Practice"``, ``"Race"``) from the YAML."""
    sess = info.get("SessionInfo") if isinstance(info, dict) else None
    sessions = sess.get("Sessions") if isinstance(sess, dict) else None
    if isinstance(sessions, list) and sessions:
        last = sessions[-1]
        if isinstance(last, dict) and last.get("SessionType"):
            return str(last["SessionType"])
    return None


def to_frame(
    values: dict[str, Any],
    *,
    t: float,
    lap_count: int,
    starts: list[float],
) -> CanonicalFrame:
    """Assemble one :class:`CanonicalFrame` from a decoded iRacing sample.

    ``lap_count`` comes from the shared :class:`~pitwall.games.iracing.normalize.LapTracker`
    (so lap boundaries match the visualiser); ``starts`` drives the computed sector index.
    """

    def num(canonical: str) -> float | None:
        raw = _first(values, _SCALAR_CANDIDATES[canonical])
        return float(raw) if raw is not None else None

    speed_ms = num("speed_kmh")
    clutch = num("clutch")
    g_lat = num("g_lat")
    g_lon = num("g_lon")
    gear = _first(values, _SCALAR_CANDIDATES["gear"])
    rpm = _first(values, _SCALAR_CANDIDATES["rpm"])
    fuel_l = num("fuel_kg")

    out: dict[str, float | None] = {
        "lap_distance_m": num("lap_distance_m"),
        "throttle": num("throttle"),
        "brake": num("brake"),
        # iRacing's Clutch is inverted (1.0 = fully disengaged); flip to canonical 1=engaged.
        "clutch": (1.0 - clutch) if clutch is not None else None,
        "steering_angle": num("steering_angle"),
        "gear": float(int(gear)) if gear is not None else None,
        "rpm": float(round(float(rpm))) if rpm is not None else None,
        "speed_kmh": speed_ms * MS_TO_KMH if speed_ms is not None else None,
        # iRacing exposes only GPS Lat/Lon/Alt, not a cartesian world frame -- left None
        # for v1 (the coach doesn't use world_*; projection is a documented follow-up).
        "world_x": None,
        "world_y": None,
        "world_z": None,
        # Sign convention to confirm against a real lap (positive = right, like ACC):
        # negate here if the live check-in shows it inverted.
        "g_lat": g_lat / G if g_lat is not None else None,
        "g_lon": g_lon / G if g_lon is not None else None,
        "fuel_kg": fuel_l * FUEL_DENSITY_KG_PER_L if fuel_l is not None else None,
    }
    for wheel in channels.WHEEL_ORDER:
        corner = _CORNER[wheel]
        temp = _first(values, _corner_candidates(corner, _TEMP_SUFFIXES))
        press = _first(values, _corner_candidates(corner, _PRESS_SUFFIXES))
        out[f"tire_temp_{wheel}"] = float(temp) if temp is not None else None
        out[f"tire_pressure_{wheel}"] = float(press) * KPA_TO_PSI if press is not None else None

    dist_pct = values.get("LapDistPct")
    return CanonicalFrame(
        t=t,
        values=out,
        lap_count=lap_count,
        sector_index=sector_index_for(float(dist_pct) if dist_pct is not None else None, starts),
        lap_valid=True,  # no per-frame invalidation flag in .ibt telemetry (documented limitation)
        in_pit=bool(values.get("OnPitRoad") or False),
        tyre_set=0,  # no reliable per-frame tyre-set channel; single-stint for v1
    )
