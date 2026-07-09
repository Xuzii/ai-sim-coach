"""iRacing .ibt reader tests.

Three layers, none needing ``pyirsdk`` or a real capture:
1. pure-function mapping/units (``mapping.to_frame`` / ``to_static_info`` / ``sector_index_for``);
2. an end-to-end run through a byte-accurate :class:`FakeIBT` (a real ``.ibt`` record buffer
   the ported ``_window_columns`` decodes for real) -> pipeline -> in-memory store;
3. an env-gated check against a real file (skipped unless ``pyirsdk`` + ``PITWALL_IRACING_IBT``).
"""

from __future__ import annotations

import os
import struct

import pytest

from pitwall.games.iracing import ibt as ibt_mod
from pitwall.games.iracing import mapping
from pitwall.games.iracing.ibt_types import IRType, mapping_for
from pitwall.games.iracing.reader import IRacingSource
from pitwall.ingest import pipeline
from pitwall.storage import db

# --------------------------------------------------------------------------- #
# A representative session-info YAML (parsed form).
# --------------------------------------------------------------------------- #
INFO = {
    "WeekendInfo": {
        "TrackName": "rudskogen",
        "TrackDisplayName": "Rudskogen Motorpark",
        "TrackLength": "3.25 km",
        "TrackAirTemp": "25.55 C",
        "TrackSurfaceTemp": "31.20 C",
    },
    "DriverInfo": {
        "DriverCarIdx": 0,
        "Drivers": [
            {"CarIdx": 0, "CarPath": "mx5 mx52016", "CarScreenName": "Mazda MX-5 Cup"},
            {"CarIdx": 1, "CarPath": "other", "CarScreenName": "Other Car"},
        ],
    },
    "SplitTimeInfo": {
        "Sectors": [
            {"SectorNum": 0, "SectorStartPct": 0.0},
            {"SectorNum": 1, "SectorStartPct": 0.33},
            {"SectorNum": 2, "SectorStartPct": 0.66},
        ]
    },
    "SessionInfo": {"Sessions": [{"SessionType": "Practice"}]},
}

TRACK_LEN_M = 3250.0


# --------------------------------------------------------------------------- #
# 1. mapping / units
# --------------------------------------------------------------------------- #
def test_to_frame_unit_conversions():
    values = {
        "LapDist": 1625.0,
        "LapDistPct": 0.5,
        "Throttle": 0.8,
        "Brake": 0.1,
        "Clutch": 0.0,  # iRacing 0 == fully engaged -> canonical 1.0
        "SteeringWheelAngle": -0.25,
        "Gear": 3,
        "RPM": 6500.4,
        "Speed": 50.0,  # m/s -> 180 km/h
        "LatAccel": 9.80665,  # 1 g
        "LongAccel": -19.6133,  # -2 g (braking)
        "FuelLevel": 40.0,  # litres
        "LFpressure": 165.0,  # kPa -> psi
        "LFtempCM": 82.0,
        "OnPitRoad": False,
    }
    f = mapping.to_frame(values, t=1.0, lap_count=2, starts=[0.0, 0.33, 0.66])
    v = f.values
    assert v["lap_distance_m"] == pytest.approx(1625.0)
    assert v["throttle"] == pytest.approx(0.8)
    assert v["clutch"] == pytest.approx(1.0)  # inverted
    assert v["gear"] == 3
    assert v["rpm"] == pytest.approx(6500.0)
    assert v["speed_kmh"] == pytest.approx(180.0)
    assert v["g_lat"] == pytest.approx(1.0, abs=1e-4)
    assert v["g_lon"] == pytest.approx(-2.0, abs=1e-3)
    assert v["fuel_kg"] == pytest.approx(40.0 * 0.745)
    assert v["tire_pressure_fl"] == pytest.approx(165.0 * 0.1450377)
    assert v["tire_temp_fl"] == pytest.approx(82.0)
    # iRacing has no cartesian world frame.
    assert v["world_x"] is None and v["world_y"] is None and v["world_z"] is None
    assert f.lap_count == 2
    assert f.sector_index == 1  # LapDistPct 0.5 is in sector 1
    assert f.in_pit is False


def test_to_frame_absent_vars_become_none():
    f = mapping.to_frame({"LapDist": 0.0, "LapDistPct": 0.0}, t=0.0, lap_count=0, starts=[])
    assert f.values["clutch"] is None
    assert f.values["speed_kmh"] is None
    assert f.values["tire_pressure_rr"] is None
    assert f.sector_index == 0  # no sector starts -> single sector


def test_resilient_pressure_name():
    # A build that uses the older `LFpress` name still maps.
    f = mapping.to_frame({"LapDist": 0.0, "LapDistPct": 0.0, "LFpress": 200.0}, t=0.0, lap_count=0, starts=[])
    assert f.values["tire_pressure_fl"] == pytest.approx(200.0 * 0.1450377)


@pytest.mark.parametrize(
    "pct, expected",
    [(0.0, 0), (0.1, 0), (0.33, 1), (0.5, 1), (0.66, 2), (0.99, 2), (None, 0)],
)
def test_sector_index_for(pct, expected):
    assert mapping.sector_index_for(pct, [0.0, 0.33, 0.66]) == expected


def test_sector_index_for_no_starts():
    assert mapping.sector_index_for(0.5, []) == 0


def test_to_static_info_from_yaml():
    si = mapping.to_static_info(INFO, started_at_utc="2026-06-04T20:14:00+00:00")
    assert si.game == "iracing"
    assert si.track_code == "rudskogen"
    assert si.track_name == "Rudskogen Motorpark"
    assert si.car_code == "mx5 mx52016"
    assert si.car_name == "Mazda MX-5 Cup"
    assert si.track_length_m == pytest.approx(3250.0)
    assert si.sector_count == 3
    assert si.session_type == "Practice"
    assert si.air_temp_c == pytest.approx(25.55)
    assert si.road_temp_c == pytest.approx(31.20)


def test_to_static_info_missing_fields_are_graceful():
    si = mapping.to_static_info({}, started_at_utc="2026-06-04T20:14:00+00:00")
    assert si.track_code == "unknown"
    assert si.car_code == "unknown"
    assert si.track_length_m is None
    assert si.sector_count == 3  # default when no SplitTimeInfo


# --------------------------------------------------------------------------- #
# 2. byte-accurate FakeIBT -> the real decode -> pipeline -> store
# --------------------------------------------------------------------------- #
# Variables packed into the fake record buffer (all scalar, count == 1).
_SPECS: list[tuple[str, IRType]] = [
    ("SessionTime", IRType.DOUBLE),
    ("SessionTick", IRType.INT),
    ("Lap", IRType.INT),
    ("LapDistPct", IRType.FLOAT),
    ("LapDist", IRType.FLOAT),
    ("OnPitRoad", IRType.BOOL),
    ("Throttle", IRType.FLOAT),
    ("Brake", IRType.FLOAT),
    ("Clutch", IRType.FLOAT),
    ("SteeringWheelAngle", IRType.FLOAT),
    ("Gear", IRType.INT),
    ("RPM", IRType.FLOAT),
    ("Speed", IRType.FLOAT),
    ("LatAccel", IRType.FLOAT),
    ("LongAccel", IRType.FLOAT),
    ("FuelLevel", IRType.FLOAT),
    ("LFpressure", IRType.FLOAT),
    ("RFpressure", IRType.FLOAT),
    ("LRpressure", IRType.FLOAT),
    ("RRpressure", IRType.FLOAT),
    ("LFtempCM", IRType.FLOAT),
    ("RFtempCM", IRType.FLOAT),
    ("LRtempCM", IRType.FLOAT),
    ("RRtempCM", IRType.FLOAT),
]


class _VH:
    def __init__(self, type_: int, count: int, offset: int) -> None:
        self.type = type_
        self.count = count
        self.offset = offset


class _VarBuf:
    def __init__(self, buf_offset: int) -> None:
        self.buf_offset = buf_offset


class _Header:
    def __init__(self, buf_len: int, buf_offset: int) -> None:
        self.buf_len = buf_len
        self.var_buf = [_VarBuf(buf_offset)]


class _DiskHeader:
    def __init__(self, n: int) -> None:
        self.session_record_count = n


class FakeIBT:
    """A minimal stand-in for ``irsdk.IBT`` with a real, decodable record buffer.

    Exposes exactly the duck-typed surface ``iracing.ibt`` reads
    (``_var_headers_dict`` / ``_header`` / ``_disk_header`` / ``_shared_mem``), so the
    ported ``_window_columns`` decodes it for real -- no ``pyirsdk`` involved.
    """

    def __init__(self, specs: list[tuple[str, IRType]], rows: list[dict]) -> None:
        offset = 0
        self._var_headers_dict: dict[str, _VH] = {}
        fmt_by_name: dict[str, str] = {}
        for name, irtype in specs:
            sc = mapping_for(irtype).struct_char
            self._var_headers_dict[name] = _VH(int(irtype), 1, offset)
            fmt_by_name[name] = sc
            offset += struct.calcsize(sc)
        buf_len = offset
        mem = bytearray()
        for row in rows:
            rec = bytearray(buf_len)
            for name, _ in specs:
                struct.pack_into(fmt_by_name[name], rec, self._var_headers_dict[name].offset, row[name])
            mem += rec
        self._header = _Header(buf_len, buf_offset=0)
        self._disk_header = _DiskHeader(len(rows))
        self._shared_mem = bytes(mem)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_rows() -> list[dict]:
    """Continuous ticks over an out-lap (Lap 0), a clean flying lap (Lap 1), and a
    sliver of Lap 2 so the flying lap closes on a real line crossing."""
    rows: list[dict] = []
    t = 0.0
    dt = 0.05  # 20 Hz; kept intact by the 50 Hz downsampler

    def emit(lap: int, n: int, *, pit_first: bool = False, last: bool = False) -> None:
        nonlocal t
        for k in range(n):
            pct = (k + 1) / (n + 1)  # strictly 0 < pct < 1, increasing
            rows.append(
                {
                    "SessionTime": t,
                    "SessionTick": int(round(t / dt)),
                    "Lap": lap,
                    "LapDistPct": pct,
                    "LapDist": pct * TRACK_LEN_M,
                    "OnPitRoad": pit_first and k == 0,
                    "Throttle": 0.85,
                    "Brake": 0.0,
                    "Clutch": 0.0,
                    "SteeringWheelAngle": 0.1,
                    "Gear": 4,
                    "RPM": 7000.0,
                    "Speed": 50.0,
                    "LatAccel": 9.80665,
                    "LongAccel": -9.80665,
                    "FuelLevel": 40.0 - lap * 0.3 - k * 0.001,
                    "LFpressure": 165.0,
                    "RFpressure": 165.0,
                    "LRpressure": 165.0,
                    "RRpressure": 165.0,
                    "LFtempCM": 80.0,
                    "RFtempCM": 80.0,
                    "LRtempCM": 80.0,
                    "RRtempCM": 80.0,
                }
            )
            t += dt

    emit(0, 30, pit_first=True)  # out-lap (starts in pit)
    emit(1, 30)  # clean flying lap
    emit(2, 3, last=True)  # sliver so lap 1 closes; trailing partial is dropped
    return rows


@pytest.fixture
def ingested(data_dir):
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    src = IRacingSource(FakeIBT(_SPECS, _make_rows()), INFO, started_at_utc="2026-06-04T20:14:00+00:00")
    session_id = pipeline.run(src, conn, target_hz=50.0, min_points=10)
    return conn, session_id


def test_end_to_end_session_metadata(ingested):
    conn, sid = ingested
    assert sid is not None
    sess = db.get_session(conn, sid)
    assert sess.game == "iracing"
    assert sess.track_name == "Rudskogen Motorpark"  # display name from the YAML
    assert sess.car_name == "Mazda MX-5 Cup"
    assert sess.track_length_m == pytest.approx(3250.0)


def test_end_to_end_laps_and_sectors(ingested):
    conn, sid = ingested
    laps = db.laps_for_session(conn, sid)
    assert len(laps) == 2  # out-lap (Lap 0) + flying lap (Lap 1); the Lap 2 sliver is dropped
    outlap = laps[0]
    assert outlap.is_outlap is True  # began on pit road

    flying = laps[1]
    assert flying.is_outlap is False
    assert flying.top_speed_kmh == pytest.approx(180.0)  # 50 m/s
    sectors = db.sectors_for_lap(conn, flying.lap_id)
    assert len(sectors) == 3
    # Forward-only partition telescopes: sector times sum to the lap time.
    assert sum(s.sector_time_ms for s in sectors) == flying.lap_time_ms


def test_frames_closes_the_ibt():
    fake = FakeIBT(_SPECS, _make_rows())
    src = IRacingSource(fake, INFO, started_at_utc="2026-06-04T20:14:00+00:00")
    list(src.frames())
    assert fake.closed is True


def test_iter_samples_skips_absent_vars():
    # Request a var the file doesn't have: it's silently skipped, not an error.
    fake = FakeIBT(_SPECS, _make_rows())
    first = next(ibt_mod.iter_samples(fake, ["Throttle", "NotARealVar", "Speed"]))
    assert "Throttle" in first and "Speed" in first
    assert "NotARealVar" not in first


# --------------------------------------------------------------------------- #
# 3. env-gated real-file smoke test (skipped in CI)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.getenv("PITWALL_IRACING_IBT"),
    reason="set PITWALL_IRACING_IBT to a real .ibt file to run the live decode test",
)
def test_real_ibt_file_ingests(data_dir):
    pytest.importorskip("irsdk")
    path = os.environ["PITWALL_IRACING_IBT"]
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    src = IRacingSource.from_ibt_file(path)
    sid = pipeline.run(src, conn)
    assert sid is not None
    assert len(db.laps_for_session(conn, sid)) >= 1
