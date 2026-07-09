"""The ACC reader: stream providers + a full replay of the committed fixture.

The replay drives the *real* ingest pipeline from a scrubbed, slimmed recording
of a Nurburgring / Ford Mustang GT3 session, so C3 stays testable in CI without
the game. The live-stream pieces use fake pages with an injected clock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pitwall.games.acc import structs as s
from pitwall.games.acc.reader import LiveFrameStream, RecordingFrameStream
from pitwall.games.acc.recording import PageKind, Record, Recording, read_recording
from pitwall.games.acc.shm import GameNotRunningError
from pitwall.ingest import pipeline
from pitwall.ingest.source import detect_source
from pitwall.storage import db, traces

FIXTURE = Path(__file__).parent / "fixtures" / "acc-nurburgring-slim.pwcap"


def _physics_bytes(packet_id: int = 0) -> bytes:
    p = s.SPageFilePhysics()
    p.packetId = packet_id
    return bytes(p)


def _graphics_bytes(status: int = int(s.AcStatus.LIVE)) -> bytes:
    g = s.SPageFileGraphic()
    g.status = status
    return bytes(g)


class FakeLivePages:
    """Pages that hand out a scripted sequence of physics packetIds, one per read."""

    def __init__(self, packets: list[int], status: int = int(s.AcStatus.LIVE)) -> None:
        self._packets = packets
        self._i = 0
        self._status = status
        self.closed = False

    def physics_bytes(self) -> bytes:
        pid = self._packets[min(self._i, len(self._packets) - 1)]
        self._i += 1
        return _physics_bytes(pid)

    def graphics_bytes(self) -> bytes:
        return _graphics_bytes(self._status)

    def static_bytes(self) -> bytes:
        return bytes(s.SPageFileStatic())

    def sizes(self) -> dict[str, int]:
        return {"static": 820, "physics": 800, "graphics": 1588}

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# RecordingFrameStream
# --------------------------------------------------------------------------- #
def test_recording_stream_pairs_latest_graphics_and_terminates():
    recording = Recording(
        meta={},
        records=[
            Record(PageKind.STATIC, 0, b"STAT"),
            Record(PageKind.GRAPHICS, 1000, b"g1"),
            Record(PageKind.PHYSICS, 1000, b"p1"),
            Record(PageKind.PHYSICS, 1500, b"p2"),
            Record(PageKind.GRAPHICS, 2000, b"g2"),
            Record(PageKind.PHYSICS, 2500, b"p3"),
        ],
    )
    stream = RecordingFrameStream(recording)
    out = list(stream)  # finite -> terminates

    assert [p for _, p, _ in out] == [b"p1", b"p2", b"p3"]
    assert [g for _, _, g in out] == [b"g1", b"g1", b"g2"]  # latest graphics with ts <= physics
    assert [round(t, 4) for t, _, _ in out] == [0.001, 0.0015, 0.0025]
    assert stream.static_bytes() == b"STAT"


# --------------------------------------------------------------------------- #
# LiveFrameStream
# --------------------------------------------------------------------------- #
def test_live_stream_yields_until_packet_freezes():
    pages = FakeLivePages([1, 2, 3, 3, 3])
    stream = LiveFrameStream(pages, stall_polls=2, sleep=lambda _s: None, clock=lambda: 0.0)
    frames = list(stream)

    assert len(frames) == 3  # packets 1, 2, 3 yielded; the frozen tail stops it
    assert pages.closed is True


def test_live_stream_stops_when_page_vanishes():
    class VanishingPages(FakeLivePages):
        def __init__(self, ok: int) -> None:
            super().__init__([1, 2, 3, 4, 5])
            self._ok = ok

        def physics_bytes(self) -> bytes:
            if self._i >= self._ok:
                raise GameNotRunningError("page gone")
            return super().physics_bytes()

    pages = VanishingPages(ok=3)
    frames = list(LiveFrameStream(pages, sleep=lambda _s: None, clock=lambda: 0.0))
    assert len(frames) == 3
    assert pages.closed is True


def test_is_running_flips_false_on_stalled_packet():
    pages = FakeLivePages([1, 2, 3, 3, 3, 3, 3])
    stream = LiveFrameStream(pages, stall_polls=3)
    results = [stream.is_running() for _ in range(7)]
    assert results[0] is True
    assert results[-1] is False


def test_is_running_false_when_not_live():
    pages = FakeLivePages([1], status=int(s.AcStatus.OFF))
    assert LiveFrameStream(pages).is_running() is False


# --------------------------------------------------------------------------- #
# ACCSource over the committed fixture, through the real pipeline
# --------------------------------------------------------------------------- #
def test_replay_fixture_through_pipeline(data_dir):
    assert FIXTURE.exists(), f"missing fixture {FIXTURE}"
    conn = db.connect(":memory:")
    db.apply_schema(conn)

    source = detect_source(recording_path=FIXTURE)
    session_id = pipeline.run(source, conn, target_hz=50.0, min_points=5)

    sess = db.get_session(conn, session_id)
    assert sess.track_code == "nurburgring"
    assert sess.track_name == "Nurburgring GP"
    assert sess.car_code == "ford_mustang_gt3"
    assert sess.car_name == "Ford Mustang GT3"
    assert sess.sector_count == 3
    # Finding 1: ACC reports trackSPlineLength == 0 in this capture, so the length
    # comes from the naming.TRACK_LENGTHS fallback -> real metres, not None/0..1.
    assert sess.track_length_m is not None
    assert 5000.0 < sess.track_length_m < 5300.0  # Nurburgring GP ~5148 m

    laps = db.laps_for_session(conn, session_id)
    assert len(laps) >= 2  # the fixture window spans a start/finish crossing
    assert any(lap.is_valid for lap in laps)  # the clean lap
    assert any(not lap.is_valid for lap in laps)  # the deliberate off-track lap

    for lap in laps:
        cols = traces.to_columns(traces.read_lap_trace(traces.resolve_trace_path(lap.trace_path)))
        assert all(0.0 <= x <= 1.0 for x in cols["throttle"])
        assert all(0.0 <= x <= 1.0 for x in cols["brake"])
        assert max(cols["speed_kmh"]) > 0.0
        assert all(-1 <= g <= 8 for g in cols["gear"])
        # Finding 1: distances are metres now, bounded by the track length.
        assert max(cols["lap_distance_m"]) > 100.0
        assert max(cols["lap_distance_m"]) <= sess.track_length_m + 50.0
        # Finding 2: the forward-only sector partition telescopes to lap_time even
        # on this real capture (where currentSectorIndex lags the lap counter).
        sector_times = [s.sector_time_ms for s in db.sectors_for_lap(conn, lap.lap_id)]
        assert abs(sum(t for t in sector_times if t is not None) - lap.lap_time_ms) <= 4


def test_replay_fixture_is_scrubbed():
    recording = read_recording(FIXTURE)
    assert recording.meta["scrubbed"] is True
    st = s.parse_static(recording.static().data)
    assert s.wstr(st.playerName) == ""
    assert s.wstr(st.playerSurname) == ""
    raw = FIXTURE.read_bytes()
    assert b"Dollahite" not in raw and b"Scott" not in raw


def test_accsource_static_info_from_fixture():
    source = detect_source(recording_path=FIXTURE)
    info = source.static_info()
    assert info.game == "acc"
    assert info.track_code == "nurburgring"
    assert info.session_type == "practice"
    assert info.air_temp_c is not None and 0.0 < info.air_temp_c < 60.0
    # Length fallback resolves at the static-info layer too (ACC reported 0).
    assert info.track_length_m is not None and 5000.0 < info.track_length_m < 5300.0


# --------------------------------------------------------------------------- #
# detect_source
# --------------------------------------------------------------------------- #
def test_detect_source_iracing_live_is_not_yet_supported():
    # iRacing now supports .ibt replay; only *live* iRacing remains a follow-up.
    with pytest.raises(NotImplementedError, match="live iRacing"):
        detect_source(game="iracing")


def test_detect_source_unknown_game_raises():
    with pytest.raises(NotImplementedError, match="unsupported game"):
        detect_source(recording_path="x.bin", game="rfactor")


def test_detect_source_live_raises_when_game_not_running():
    from pitwall.games.acc.shm import PHYSICS, mapping_exists

    if mapping_exists(PHYSICS):
        pytest.skip("ACC is running on this machine; cannot exercise the not-running path")
    with pytest.raises(GameNotRunningError):
        detect_source()
