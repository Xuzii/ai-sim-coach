"""C3 tests: the ACC raw-bytes capture tool, exercised entirely offline.

No game and no real waiting: the capture loop runs against a fake page provider
with an injected clock, and the "game not running" path is checked against a
shared-memory name that can never exist.
"""

from __future__ import annotations

import pytest

from pitwall.games.acc import shm
from pitwall.games.acc.capture import capture_session
from pitwall.games.acc.recording import MAGIC, PageKind, RecordingWriter, read_recording


class FakePages:
    """A stand-in for LivePages: canned bytes, counting physics frames so each is distinct."""

    def __init__(self) -> None:
        self._n = 0
        self.closed = False

    def sizes(self) -> dict[str, int]:
        return {"static": 8, "physics": 4, "graphics": 6}

    def static_bytes(self) -> bytes:
        return b"STATIC00"

    def physics_bytes(self) -> bytes:
        self._n += 1
        return self._n.to_bytes(4, "big")

    def graphics_bytes(self) -> bytes:
        return b"GRAPHX"

    def close(self) -> None:
        self.closed = True


def _clock_from(values: list[float]):
    it = iter(values)
    return lambda: next(it)


def test_recording_round_trip(tmp_path):
    path = tmp_path / "rt.pwcap"
    with RecordingWriter(path, page_sizes={"static": 3, "physics": 2, "graphics": 1}, poll_hz=50.0,
                         started_at_utc="2026-05-23T14:00:00+00:00") as w:
        w.write_static(0, b"abc")
        w.write_physics(1000, b"p1")
        w.write_graphics(1000, b"g")
        w.write_physics(2000, b"p2")

    rec = read_recording(path)
    assert rec.meta["poll_hz"] == 50.0
    assert rec.meta["page_sizes"] == {"static": 3, "physics": 2, "graphics": 1}
    assert rec.meta["scrubbed"] is False
    assert rec.static().data == b"abc"
    phys = rec.of_kind(PageKind.PHYSICS)
    assert [r.data for r in phys] == [b"p1", b"p2"]
    assert [r.ts_micros for r in phys] == [1000, 2000]
    assert [r.data for r in rec.of_kind(PageKind.GRAPHICS)] == [b"g"]


def test_capture_loop_offline(tmp_path):
    path = tmp_path / "cap.pwcap"
    pages = FakePages()
    # start=0.0, then three ticks below the 1.0s duration, then one at/over it -> 3 frames.
    clock = _clock_from([0.0, 0.1, 0.2, 0.3, 1.0])

    stats = capture_session(
        path, duration_s=1.0, poll_hz=100.0, pages=pages,
        clock=clock, sleep=lambda _s: None, progress=None,
    )

    assert stats.physics_samples == 3
    assert stats.graphics_samples == 3
    assert stats.page_sizes == {"static": 8, "physics": 4, "graphics": 6}
    assert pages.closed is False  # caller-owned provider is left open

    rec = read_recording(path)
    assert rec.static().data == b"STATIC00"
    phys = rec.of_kind(PageKind.PHYSICS)
    assert [r.data for r in phys] == [(1).to_bytes(4, "big"), (2).to_bytes(4, "big"), (3).to_bytes(4, "big")]
    ts = [r.ts_micros for r in phys]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)  # strictly monotonic
    assert rec.meta["game"] == "acc" and rec.meta["pitwall_version"]


def test_missing_page_raises_game_not_running():
    name = r"Local\pitwall_does_not_exist_zzz"
    assert shm.mapping_exists(name) is False
    with pytest.raises(shm.GameNotRunningError):
        shm.open_page(name, 64)
    with pytest.raises(shm.GameNotRunningError):
        shm.probe_size(name)


def test_capture_raises_when_game_not_running(tmp_path):
    # No injected provider -> opens real LivePages, which fails fast off-game.
    with pytest.raises(shm.GameNotRunningError):
        capture_session(tmp_path / "x.pwcap", duration_s=0.1, progress=None)


def test_bad_magic_rejected(tmp_path):
    bad = tmp_path / "bad.pwcap"
    bad.write_bytes(b"NOTPWCAP" + b"\x00" * 16)
    with pytest.raises(ValueError, match="magic"):
        read_recording(bad)


def test_truncated_recording_rejected(tmp_path):
    path = tmp_path / "full.pwcap"
    with RecordingWriter(path, page_sizes={"physics": 4}, poll_hz=10.0,
                         started_at_utc="2026-05-23T14:00:00+00:00") as w:
        w.write_physics(0, b"data")
    raw = path.read_bytes()
    assert raw[: len(MAGIC)] == MAGIC
    truncated = tmp_path / "trunc.pwcap"
    truncated.write_bytes(raw[:-2])  # chop the tail of the payload
    with pytest.raises(ValueError, match="truncated"):
        read_recording(truncated)
