"""C0 tests: canonical schema, name normalisation, timestamp handling."""

from __future__ import annotations

from datetime import datetime, timezone

from pitwall import channels, naming, timeutil

CANONICAL_SET = {
    "throttle", "brake", "clutch", "steering_angle", "gear", "rpm", "speed_kmh",
    "lap_distance_m", "world_x", "world_y", "world_z",
    "tire_temp_fl", "tire_temp_fr", "tire_temp_rl", "tire_temp_rr",
    "tire_pressure_fl", "tire_pressure_fr", "tire_pressure_rl", "tire_pressure_rr",
    "g_lat", "g_lon", "fuel_kg",
}


def test_canonical_channels_match_spec_and_are_unique():
    names = channels.CHANNEL_NAMES
    assert len(names) == len(set(names)), "duplicate channel name"
    assert set(names) == CANONICAL_SET


def test_distance_key_is_a_canonical_channel():
    assert channels.DISTANCE_KEY in channels.CHANNEL_NAMES
    assert channels.CHANNEL_NAMES[0] == channels.DISTANCE_KEY


def test_every_channel_has_dtype_unit_description():
    for ch in channels.CANONICAL_CHANNELS:
        assert ch.dtype in {"float32", "int32", "int8"}
        assert ch.unit
        assert ch.description


def test_trace_columns_lead_with_distance_then_time():
    cols = channels.trace_columns()
    assert cols[0] == channels.DISTANCE_KEY
    assert cols[1] == channels.TIME_KEY
    assert set(cols) == set(channels.CHANNEL_NAMES) | {channels.TIME_KEY}
    assert len(cols) == len(set(cols)), "duplicate trace column"


def test_dtype_for_handles_time_key_and_channels():
    assert channels.dtype_for(channels.TIME_KEY) == "int32"
    assert channels.dtype_for("gear") == "int8"
    assert channels.dtype_for("throttle") == "float32"


def test_tire_groupings_in_wheel_order():
    assert channels.TIRE_TEMP_CHANNELS == (
        "tire_temp_fl", "tire_temp_fr", "tire_temp_rl", "tire_temp_rr",
    )
    assert channels.TIRE_PRESSURE_CHANNELS == (
        "tire_pressure_fl", "tire_pressure_fr", "tire_pressure_rl", "tire_pressure_rr",
    )


def test_naming_known_codes():
    assert naming.track_name("spa") == "Spa-Francorchamps"
    assert naming.car_name("mclaren_720s_gt3").lower().startswith("mclaren")


def test_naming_fallback_never_raises():
    assert naming.track_name("some_new_track") == "Some New Track"
    assert naming.track_name("") == "Unknown"
    assert naming.track_name(None) == "Unknown"
    assert naming.car_name(None) == "Unknown"


def test_timeutil_roundtrip_preserves_utc_instant():
    dt = datetime(2026, 5, 23, 14, 30, tzinfo=timezone.utc)
    back = timeutil.from_iso_utc(timeutil.to_iso_utc(dt))
    assert back == dt
    assert back.tzinfo is not None


def test_timeutil_naive_is_assumed_utc():
    naive = datetime(2026, 5, 23, 14, 30)
    restored = timeutil.from_iso_utc(timeutil.to_iso_utc(naive))
    assert restored == naive.replace(tzinfo=timezone.utc)
