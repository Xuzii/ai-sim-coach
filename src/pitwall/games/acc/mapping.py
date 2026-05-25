"""ACC shared-memory fields -> the canonical schema.

Pure functions: they take already-parsed :mod:`~pitwall.games.acc.structs`
objects and return :class:`~pitwall.ingest.frames.StaticInfo` /
:class:`~pitwall.ingest.frames.CanonicalFrame`. No I/O, no mmap, no ctypes
parsing -- which makes every conversion (gear-1, fuel litres->kg, the distance
key, the wheel order) a one-line unit test. The reader (:mod:`reader`) owns the
struct parsing and timing; this module owns only the field-by-field translation
documented in ``docs/channel-mapping.md``.
"""

from __future__ import annotations

from pitwall import channels
from pitwall.games.acc.structs import (
    SPageFileGraphic,
    SPageFilePhysics,
    SPageFileStatic,
    session_type_name,
    wstr,
)
from pitwall.ingest.frames import CanonicalFrame, StaticInfo
from pitwall.naming import track_length_m as _track_length_lookup

#: ACC reports fuel in litres; GT3 racing petrol is ~0.745 kg/L. Single source of
#: truth for the litres->kg conversion (referenced from docs/channel-mapping.md).
FUEL_DENSITY_KG_PER_L = 0.745


def _has_length(track_length_m: float | None) -> bool:
    """ACC reports 0.0 for trackSPlineLength in some states; treat <=1 as unknown."""
    return track_length_m is not None and track_length_m > 1.0


def resolve_track_length(reported_length_m: float | None, track_code: str) -> float | None:
    """Track length in metres: trust ACC's value when valid, else fall back to the
    :data:`pitwall.naming.TRACK_LENGTHS` lookup by code, else None (keeps the 0..1
    distance key). Single source of truth so the session row (via
    :func:`to_static_info`) and every per-frame ``lap_distance_m`` (via the reader)
    agree -- they must, or sector distance bounds and trace distances diverge."""
    if _has_length(reported_length_m):
        return float(reported_length_m)
    return _track_length_lookup(track_code)


def to_static_info(
    static: SPageFileStatic,
    *,
    started_at_utc: str,
    game: str = "acc",
    session_type: str | None = None,
    air_temp_c: float | None = None,
    road_temp_c: float | None = None,
) -> StaticInfo:
    """Build the once-per-session :class:`StaticInfo` from the static page.

    ``session_type`` and the temperatures live on the graphics/physics pages, not
    the static one, so the reader passes them in after peeking the first frame.
    ``track_length_m`` falls back to the :data:`pitwall.naming.TRACK_LENGTHS` lookup
    when ACC reports 0, and is ``None`` only for an unknown track (the reader/mapping
    then fall back to a normalised 0..1 distance key -- see :func:`lap_distance_m`).
    """
    track_code = wstr(static.track)
    return StaticInfo(
        game=game,
        track_code=track_code,
        car_code=wstr(static.carModel),
        started_at_utc=started_at_utc,
        sector_count=static.sectorCount or 3,
        track_length_m=resolve_track_length(static.trackSPlineLength, track_code),
        session_type=session_type,
        air_temp_c=air_temp_c,
        road_temp_c=road_temp_c,
    )


def lap_distance_m(graphics: SPageFileGraphic, track_length_m: float | None) -> float:
    """Distance along the lap from the start/finish line, in metres.

    ``normalizedCarPosition`` (0..1) x track length. When ACC reports no track
    length (0), fall back to the raw 0..1 fraction so the distance key stays
    monotonic within a lap and sector slicing still works -- the units are then
    fractions, not metres, but ordering and binning are unaffected.
    """
    norm = float(graphics.normalizedCarPosition)
    if _has_length(track_length_m):
        return norm * float(track_length_m)
    return norm


def _player_coords(graphics: SPageFileGraphic) -> tuple[float, float, float]:
    """World coordinates of the player's car (``carCoordinates[playerCarID]``)."""
    i = graphics.playerCarID if 0 <= graphics.playerCarID < 60 else 0
    c = graphics.carCoordinates[i]
    return float(c[0]), float(c[1]), float(c[2])


def to_frame(
    physics: SPageFilePhysics,
    graphics: SPageFileGraphic,
    *,
    t: float,
    track_length_m: float | None,
) -> CanonicalFrame:
    """Assemble one :class:`CanonicalFrame` from a physics+graphics snapshot."""
    wx, wy, wz = _player_coords(graphics)
    values: dict[str, float] = {
        "lap_distance_m": lap_distance_m(graphics, track_length_m),
        "throttle": float(physics.gas),
        "brake": float(physics.brake),
        "clutch": float(physics.clutch),
        "steering_angle": float(physics.steerAngle),
        "gear": float(physics.gear - 1),  # ACC 0=R,1=N,2=1st -> -1,0,1 for display
        "rpm": float(physics.rpms),
        "speed_kmh": float(physics.speedKmh),
        "world_x": wx,
        "world_y": wy,
        "world_z": wz,
        # accG axis/sign confirmed against a real session (C3): accG[2] is
        # longitudinal (negative under braking, positive on power); accG[0] is
        # lateral but signed opposite to steerAngle, so negate it -> g_lat shares
        # sign with steering_angle (positive = right).
        "g_lat": -float(physics.accG[0]),
        "g_lon": float(physics.accG[2]),
        "fuel_kg": float(physics.fuel) * FUEL_DENSITY_KG_PER_L,
    }
    # Tyre arrays are ACC index order FL, FR, RL, RR == channels.WHEEL_ORDER.
    for i, wheel in enumerate(channels.WHEEL_ORDER):
        values[f"tire_temp_{wheel}"] = float(physics.tyreCoreTemperature[i])
        values[f"tire_pressure_{wheel}"] = float(physics.wheelsPressure[i])

    return CanonicalFrame(
        t=t,
        values=values,
        lap_count=int(graphics.completedLaps),
        sector_index=int(graphics.currentSectorIndex),
        lap_valid=bool(graphics.isValidLap),
        in_pit=bool(graphics.isInPitLane),  # whole pit-lane traversal -> out/in-lap flags
        tyre_set=int(graphics.currentTyreSet),
    )


def session_type_from(graphics: SPageFileGraphic) -> str | None:
    """The lowercase session-type label from the graphics page, or None."""
    return session_type_name(int(graphics.session))
