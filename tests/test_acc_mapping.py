"""Pure ACC->canonical conversions, on handcrafted structs (no game, no file)."""

from __future__ import annotations

import ctypes

import pytest

from pitwall.games.acc import mapping
from pitwall.games.acc import structs as s


def _set_w(obj: ctypes.Structure, field: str, text: str) -> None:
    """Write a UTF-16-LE string into a wchar field of a struct instance."""
    enc = text.encode("utf-16-le")
    ctypes.memmove(getattr(obj, field), enc, len(enc))


def _physics(**kw) -> s.SPageFilePhysics:
    p = s.SPageFilePhysics()
    arrays = {"accG", "tyreCoreTemperature", "wheelsPressure"}
    for key, value in kw.items():
        if key in arrays:
            for i, v in enumerate(value):
                getattr(p, key)[i] = v
        else:
            setattr(p, key, value)
    return p


def _graphics(**kw) -> s.SPageFileGraphic:
    g = s.SPageFileGraphic()
    for key, value in kw.items():
        if key == "coords":  # (carId, (x, y, z))
            car_id, xyz = value
            for i, v in enumerate(xyz):
                g.carCoordinates[car_id][i] = v
        else:
            setattr(g, key, value)
    return g


def test_gear_minus_one():
    for acc_gear, canonical in [(0, -1.0), (1, 0.0), (2, 1.0), (7, 6.0)]:
        frame = mapping.to_frame(_physics(gear=acc_gear), _graphics(), t=0.0, track_length_m=1000.0)
        assert frame.values["gear"] == canonical


def test_fuel_litres_to_kg():
    frame = mapping.to_frame(_physics(fuel=10.0), _graphics(), t=0.0, track_length_m=1000.0)
    assert frame.values["fuel_kg"] == pytest.approx(7.45)


def test_lap_distance_uses_length():
    g = _graphics(normalizedCarPosition=0.5)
    assert mapping.lap_distance_m(g, 7000.0) == pytest.approx(3500.0)


def test_lap_distance_falls_back_to_normalized():
    g = _graphics(normalizedCarPosition=0.5)
    assert mapping.lap_distance_m(g, None) == pytest.approx(0.5)
    assert mapping.lap_distance_m(g, 0.0) == pytest.approx(0.5)  # ACC reports 0 -> fallback


def test_wheel_order_fl_fr_rl_rr():
    p = _physics(tyreCoreTemperature=[1.0, 2.0, 3.0, 4.0], wheelsPressure=[5.0, 6.0, 7.0, 8.0])
    v = mapping.to_frame(p, _graphics(), t=0.0, track_length_m=1000.0).values
    assert (v["tire_temp_fl"], v["tire_temp_fr"], v["tire_temp_rl"], v["tire_temp_rr"]) == (1.0, 2.0, 3.0, 4.0)
    assert (v["tire_pressure_fl"], v["tire_pressure_fr"], v["tire_pressure_rl"], v["tire_pressure_rr"]) == (
        5.0, 6.0, 7.0, 8.0,
    )


def test_accg_axes():
    p = _physics(accG=[0.9, 0.1, -1.2])
    v = mapping.to_frame(p, _graphics(), t=0.0, track_length_m=1000.0).values
    assert v["g_lat"] == pytest.approx(-0.9)  # -accG[0] (positive = right, matching steering)
    assert v["g_lon"] == pytest.approx(-1.2)  # accG[2] (negative = braking)


def test_player_coords_indexed_by_player_car_id():
    g = _graphics(playerCarID=3, coords=(3, (10.0, 20.0, 30.0)))
    v = mapping.to_frame(_physics(), g, t=0.0, track_length_m=1000.0).values
    assert (v["world_x"], v["world_y"], v["world_z"]) == (10.0, 20.0, 30.0)


def test_player_coords_out_of_range_falls_back_to_zero_index():
    g = _graphics(playerCarID=99, coords=(0, (1.0, 2.0, 3.0)))
    v = mapping.to_frame(_physics(), g, t=0.0, track_length_m=1000.0).values
    assert (v["world_x"], v["world_y"], v["world_z"]) == (1.0, 2.0, 3.0)


def test_segmentation_fields_pass_through():
    g = _graphics(completedLaps=4, currentSectorIndex=2, isValidLap=0, isInPitLane=1, currentTyreSet=3)
    frame = mapping.to_frame(_physics(), g, t=1.5, track_length_m=1000.0)
    assert frame.t == 1.5
    assert frame.lap_count == 4
    assert frame.sector_index == 2
    assert frame.lap_valid is False
    assert frame.in_pit is True
    assert frame.tyre_set == 3


def test_frame_has_all_canonical_channels():
    from pitwall import channels

    v = mapping.to_frame(_physics(), _graphics(), t=0.0, track_length_m=1000.0).values
    assert set(v) == set(channels.CHANNEL_NAMES)


def test_to_static_info():
    st = s.SPageFileStatic()
    _set_w(st, "track", "nurburgring")
    _set_w(st, "carModel", "ford_mustang_gt3")
    st.sectorCount = 3
    st.trackSPlineLength = 0.0  # ACC sometimes reports 0
    info = mapping.to_static_info(st, started_at_utc="2026-05-24T20:00:00+00:00")
    assert info.game == "acc"
    assert info.track_code == "nurburgring"
    assert info.car_code == "ford_mustang_gt3"
    assert info.sector_count == 3
    # 0 -> fall back to the naming.TRACK_LENGTHS lookup so distances are metres.
    assert info.track_length_m == pytest.approx(5148.0)


def test_to_static_info_keeps_real_length():
    st = s.SPageFileStatic()
    _set_w(st, "track", "spa")
    st.sectorCount = 3
    st.trackSPlineLength = 7004.0
    info = mapping.to_static_info(st, started_at_utc="2026-05-24T20:00:00+00:00")
    assert info.track_length_m == pytest.approx(7004.0)


def test_to_static_info_unknown_track_stays_none():
    # Unknown code + ACC reporting 0 -> None, so distances keep the 0..1 key
    # rather than guessing a length.
    st = s.SPageFileStatic()
    _set_w(st, "track", "some_new_dlc_track")
    st.sectorCount = 3
    st.trackSPlineLength = 0.0
    info = mapping.to_static_info(st, started_at_utc="2026-05-24T20:00:00+00:00")
    assert info.track_length_m is None
