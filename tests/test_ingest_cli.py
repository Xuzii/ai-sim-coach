"""Tests for the ``pitwall-ingest`` command, exercised entirely offline.

No game and no real waiting: replay mode runs the committed fixture, the live
``ingest_once``/``run_watch`` paths use a synthetic source with ACC's
``detect_source``/``mapping_exists`` monkeypatched, and the wait-for-live poll
runs against injected clock/sleep. Mirrors ``test_acc_capture.py`` style.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from pitwall.games.acc.shm import GameNotRunningError
from pitwall.ingest import cli, pipeline
from pitwall.ingest.frames import CanonicalFrame, StaticInfo
from pitwall.ingest.source import TelemetrySource
from pitwall.ingest.synthetic import SyntheticACCSource
from pitwall.storage import db

FIXTURE = Path(__file__).parent / "fixtures" / "acc-nurburgring-slim.pwcap"
# Same small, fast synthetic session the ingest tests use (8 laps, Spa / 720S).
TEST_KWARGS = dict(track_length_m=2400.0, native_hz=150.0)


class _NoLapSource(TelemetrySource):
    """A live source that goes LIVE but yields no frames -- e.g. the driver leaves
    before completing a lap. ``pipeline.run`` must return None and write nothing."""

    native_hz = 150.0

    def static_info(self) -> StaticInfo:
        return StaticInfo(
            game="acc", track_code="spa", car_code="mclaren_720s_gt3",
            started_at_utc="2026-05-27T00:00:00Z",
        )

    def frames(self) -> Iterator[CanonicalFrame]:
        return iter(())


def _clock_from(values: list[float]):
    it = iter(values)
    return lambda: next(it)


# --------------------------------------------------------------------------- #
# Replay mode through main()
# --------------------------------------------------------------------------- #
def test_main_ingests_recording(data_dir):
    assert FIXTURE.exists(), f"missing fixture {FIXTURE}"
    rc = cli.main([str(FIXTURE)])
    assert rc == 0

    # Re-open the (now on-disk) store the command wrote and confirm it's populated.
    conn = db.connect()
    sessions = db.find_sessions(conn)
    assert len(sessions) == 1
    laps = db.laps_for_session(conn, sessions[0].session_id)
    assert len(laps) >= 2  # the fixture window spans a start/finish crossing
    assert any(lap.is_valid for lap in laps)
    assert any(not lap.is_valid for lap in laps)
    conn.close()


def test_main_missing_file_returns_2(data_dir, capsys):
    rc = cli.main(["does_not_exist.pwcap"])
    assert rc == 2
    assert "no such file" in capsys.readouterr().err


def test_main_corrupt_recording_returns_2(tmp_path, data_dir, capsys):
    bad = tmp_path / "bad.pwcap"
    bad.write_bytes(b"NOTPWCAP" + b"\x00" * 16)
    rc = cli.main([str(bad)])
    assert rc == 2
    assert "magic" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# on_lap progress hook (the one pipeline.run change)
# --------------------------------------------------------------------------- #
def test_on_lap_fires_once_per_lap():
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    captured: list[tuple[int, int | None]] = []
    pipeline.run(
        SyntheticACCSource(seed=7, **TEST_KWARGS),
        conn,
        on_lap=lambda n, t: captured.append((n, t)),
    )
    assert [n for n, _ in captured] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert all(isinstance(t, int) and t > 0 for _, t in captured)


def test_on_lap_default_none_is_inert():
    """A no-op on_lap must persist byte-identical rows to omitting it entirely."""

    def rows(on_lap):
        conn = db.connect(":memory:")
        db.apply_schema(conn)
        sid = pipeline.run(SyntheticACCSource(seed=7, **TEST_KWARGS), conn, on_lap=on_lap)
        return [
            (lap.lap_number, lap.lap_time_ms, lap.point_count, lap.is_valid, lap.stint_number)
            for lap in db.laps_for_session(conn, sid)
        ]

    assert rows(None) == rows(lambda n, t: None)


# --------------------------------------------------------------------------- #
# _summarize
# --------------------------------------------------------------------------- #
def test_summarize_line(data_dir):
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    sid = pipeline.run(SyntheticACCSource(seed=7, **TEST_KWARGS), conn)
    line = cli._summarize(conn, sid)
    assert "Spa-Francorchamps" in line
    assert "McLaren 720S GT3" in line
    assert "8 lap(s) ingested (7 valid)" in line  # lap 5 is the deliberate invalid one


# --------------------------------------------------------------------------- #
# _wait_for_live  (gates on session status, not page existence)
# --------------------------------------------------------------------------- #
def test_wait_for_live_returns_when_session_goes_live():
    calls = {"n": 0}

    def live() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3  # not live, not live, then live (on track)

    ok = cli._wait_for_live(
        is_live=live, clock=lambda: 0.0, sleep=lambda _s: None, progress=None
    )
    assert ok is True
    assert calls["n"] == 3


def test_wait_for_live_times_out():
    ok = cli._wait_for_live(
        is_live=lambda: False,
        clock=_clock_from([0.0, 1.0]),  # start=0.0, first check=1.0 >= stop_after
        sleep=lambda _s: None,
        progress=None,
        stop_after_s=0.5,
    )
    assert ok is False


# --------------------------------------------------------------------------- #
# run_watch (loop reuses detect_source + pipeline.run per session)
#
# ``is_live`` is injected (the real one reads ACC's graphics status); the loop
# gates on a LIVE session, never on bare page existence -- see the menu-spin
# regression test below.
# --------------------------------------------------------------------------- #
def test_run_watch_loops_until_max_sessions(monkeypatch):
    monkeypatch.setattr(
        "pitwall.ingest.cli.detect_source",
        lambda *, recording_path=None: SyntheticACCSource(seed=7, **TEST_KWARGS),
    )
    conn = db.connect(":memory:")
    db.apply_schema(conn)

    n = cli.run_watch(
        conn, max_sessions=2, sleep=lambda _s: None, clock=lambda: 0.0, progress=None,
        is_live=lambda: True,
    )
    assert n == 2
    assert len(db.find_sessions(conn)) == 2


def test_run_watch_survives_a_failed_session(monkeypatch):
    calls = {"n": 0}

    def flaky(*, recording_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient shared-memory hiccup")  # not a KeyboardInterrupt
        return SyntheticACCSource(seed=7, **TEST_KWARGS)

    monkeypatch.setattr("pitwall.ingest.cli.detect_source", flaky)
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    msgs: list[str] = []

    n = cli.run_watch(
        conn, max_sessions=1, sleep=lambda _s: None, clock=lambda: 0.0, progress=msgs.append,
        is_live=lambda: True,
    )
    assert n == 1  # recovered: the good session after the failed one still counted
    assert any("session ingest failed" in m for m in msgs)  # the failure was logged
    assert len(db.find_sessions(conn)) == 1


def test_run_watch_keyboardinterrupt_keeps_committed_laps(monkeypatch):
    calls = {"n": 0}

    def flaky(*, recording_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return SyntheticACCSource(seed=7, **TEST_KWARGS)  # ingests one full session
        raise KeyboardInterrupt  # Ctrl-C before the next session

    monkeypatch.setattr("pitwall.ingest.cli.detect_source", flaky)
    conn = db.connect(":memory:")
    db.apply_schema(conn)

    n = cli.run_watch(
        conn, max_sessions=5, sleep=lambda _s: None, clock=lambda: 0.0, progress=None,
        is_live=lambda: True,
    )
    assert n == 1  # interrupt broke the loop cleanly, no propagation
    sessions = db.find_sessions(conn)
    assert len(sessions) == 1
    assert len(db.laps_for_session(conn, sessions[0].session_id)) == 8  # session 1 fully preserved


def test_run_watch_never_ingests_while_not_live(monkeypatch):
    """Regression: ACC open but not on track (status != LIVE) must ingest nothing.

    The original daemon gated on shared-memory page existence, which is true the
    moment ACC launches -- so parking in the menu spun out thousands of 0-lap
    session rows. With status-gating, ``detect_source`` is never even reached while
    not live. (We Ctrl-C out of the wait to end the otherwise-blocking loop, exactly
    as a user would after giving up in the menu.)
    """
    detect_calls = {"n": 0}

    def detect(*, recording_path=None):
        detect_calls["n"] += 1
        return SyntheticACCSource(seed=7, **TEST_KWARGS)

    monkeypatch.setattr("pitwall.ingest.cli.detect_source", detect)
    polls = {"n": 0}

    def live() -> bool:
        polls["n"] += 1
        if polls["n"] >= 3:
            raise KeyboardInterrupt  # user gives up waiting in the menu
        return False  # never on track

    conn = db.connect(":memory:")
    db.apply_schema(conn)
    n = cli.run_watch(
        conn, max_sessions=5, sleep=lambda _s: None, clock=lambda: 0.0,
        progress=None, is_live=live,
    )
    assert detect_calls["n"] == 0  # never tried to ingest while not on track
    assert db.find_sessions(conn) == []
    assert n == 0


def test_run_watch_skips_no_lap_session(monkeypatch):
    """A LIVE session that produces no complete lap writes no row and isn't counted.

    Belt-and-suspenders to the status gate: even when on track, a driver who leaves
    before finishing a lap (0 frames -> ``pipeline.run`` returns None) must not leave
    an empty session behind. The next real session still ingests normally.
    """
    sources = iter([_NoLapSource(), SyntheticACCSource(seed=7, **TEST_KWARGS)])
    monkeypatch.setattr(
        "pitwall.ingest.cli.detect_source", lambda *, recording_path=None: next(sources)
    )
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    msgs: list[str] = []

    n = cli.run_watch(
        conn, max_sessions=1, sleep=lambda _s: None, clock=lambda: 0.0,
        progress=msgs.append, is_live=lambda: True,
    )
    assert n == 1  # the no-lap session was not counted; only the real one was
    assert len(db.find_sessions(conn)) == 1  # and no empty row was persisted
    assert any("no complete lap" in m for m in msgs)


# --------------------------------------------------------------------------- #
# watch-mode logging
# --------------------------------------------------------------------------- #
def test_watch_progress_writes_timestamped_log(tmp_path):
    log = tmp_path / "ingest.log"
    emit = cli._make_watch_progress(log)
    emit("hello world")
    content = log.read_text(encoding="utf-8")
    assert "hello world" in content
    assert content.startswith("[")  # ISO-UTC timestamp prefix


def test_watch_progress_none_log_does_not_write(capsys):
    emit = cli._make_watch_progress(None)
    emit("stdout only")  # must not raise without a log path
    assert "stdout only" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# argv guards
# --------------------------------------------------------------------------- #
def test_main_watch_with_recording_rejected(capsys):
    rc = cli.main(["session.pwcap", "--watch"])
    assert rc == 2
    assert "--watch only applies to live" in capsys.readouterr().err


def test_main_game_not_running_returns_2(data_dir, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise GameNotRunningError("ACC is not running -- launch ACC and enter a session first.")

    monkeypatch.setattr("pitwall.ingest.cli.detect_source", boom)
    rc = cli.main([])  # live: no recording path
    assert rc == 2
    assert "ACC is not running" in capsys.readouterr().err
