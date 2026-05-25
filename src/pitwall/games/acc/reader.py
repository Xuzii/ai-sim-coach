"""The ACC telemetry source: raw shared-memory bytes -> canonical frames.

:class:`ACCSource` is the live counterpart to the synthetic source -- it
satisfies the same :class:`~pitwall.ingest.source.TelemetrySource` contract, so
the downsample/segment/persist pipeline ingests it with zero changes.

The trick to de-risking it is one ``frames()`` body fed by two interchangeable
providers behind :class:`FrameStream`:

* :class:`RecordingFrameStream` replays a captured ``.pwcap`` offline -- this is
  how the whole reader is developed and regression-tested without the game. (It
  supersedes the "RecordingPages" name floated in the milestone notes; replay is
  a *stream* of pre-timestamped page pairs, not a poll-driven snapshot provider.)
* :class:`LiveFrameStream` polls :class:`~pitwall.games.acc.shm.LivePages` while
  ACC runs, and -- critically -- **terminates when the session ends** (status
  leaves LIVE, the physics ``packetId`` freezes, or the page vanishes), because
  ``pipeline.run`` drains ``frames()`` fully before it persists anything.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Protocol

from pitwall.games.acc import mapping
from pitwall.games.acc.recording import PageKind, Recording
from pitwall.games.acc.shm import GameNotRunningError
from pitwall.games.acc.structs import AcStatus, parse_graphics, parse_physics, parse_static, wstr
from pitwall.ingest.frames import CanonicalFrame, StaticInfo
from pitwall.ingest.source import TelemetrySource


class FrameStream(Protocol):
    """A time-ordered stream of paired raw page bytes, plus the static page.

    ``__iter__`` yields ``(t_seconds, physics_bytes, graphics_bytes)`` and stops
    when the session ends. ``static_bytes`` returns the once-per-session page.
    """

    def static_bytes(self) -> bytes: ...
    def __iter__(self) -> Iterator[tuple[float, bytes, bytes]]: ...
    def close(self) -> None: ...


class RecordingFrameStream:
    """Replays a parsed ``.pwcap`` :class:`Recording` as a frame stream (offline).

    Pairs each physics record with the most recent graphics record captured at or
    before it (a merge-join on the monotonic ``ts_micros``), using ``ts_micros /
    1e6`` as the frame time. Terminates naturally at end-of-recording.
    """

    def __init__(self, recording: Recording) -> None:
        self._recording = recording
        self._physics = recording.of_kind(PageKind.PHYSICS)
        self._graphics = recording.of_kind(PageKind.GRAPHICS)
        static = recording.static()
        self._static_bytes = static.data if static is not None else b""

    def static_bytes(self) -> bytes:
        return self._static_bytes

    def __iter__(self) -> Iterator[tuple[float, bytes, bytes]]:
        graphics = self._graphics
        gi = 0
        n = len(graphics)
        latest: bytes | None = None
        for p in self._physics:
            while gi < n and graphics[gi].ts_micros <= p.ts_micros:
                latest = graphics[gi].data
                gi += 1
            if latest is None:
                # No graphics captured yet; carry the first one if it exists.
                if n == 0:
                    continue
                latest = graphics[0].data
            yield (p.ts_micros / 1_000_000.0, p.data, latest)

    def close(self) -> None:  # nothing to release; here to satisfy FrameStream
        pass


class LiveFrameStream:
    """Polls ACC's live pages, yielding frames until the session ends.

    ``clock``/``sleep`` are injectable so the loop runs in tests without real
    time (mirroring :func:`pitwall.games.acc.capture.capture_session`). Duplicate
    physics packets (poll rate above the physics tick) are skipped, so ``t`` stays
    clean. ``is_running`` is stateful: it returns False once the game leaves LIVE
    or the physics ``packetId`` has been frozen for ``stall_polls`` checks.
    """

    def __init__(
        self,
        pages,
        *,
        poll_hz: float = 100.0,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
        stall_polls: int = 300,
        own_pages: bool = True,
    ) -> None:
        self._pages = pages
        self._period = 1.0 / poll_hz
        self._clock = clock
        self._sleep = sleep
        self._stall_polls = stall_polls
        self._own_pages = own_pages
        self._last_packet: int | None = None
        self._stall = 0

    def static_bytes(self) -> bytes:
        return self._pages.static_bytes()

    def is_running(self) -> bool:
        """A one-shot, stateful liveness check (for callers polling outside the loop).

        Returns False once the game leaves LIVE, the page vanishes, or the physics
        ``packetId`` has been frozen for ``stall_polls`` successive calls. The
        iterator does its own inline termination and does not call this.
        """
        try:
            graphics = parse_graphics(self._pages.graphics_bytes())
            physics = parse_physics(self._pages.physics_bytes())
        except GameNotRunningError:
            return False
        if graphics.status != AcStatus.LIVE:
            return False
        if physics.packetId == self._last_packet:
            self._stall += 1
        else:
            self._stall = 0
            self._last_packet = physics.packetId
        return self._stall < self._stall_polls

    def __iter__(self) -> Iterator[tuple[float, bytes, bytes]]:
        start = self._clock()
        last_packet: int | None = None
        stall = 0
        try:
            while True:
                try:
                    physics_bytes = self._pages.physics_bytes()
                    graphics_bytes = self._pages.graphics_bytes()
                except GameNotRunningError:
                    break  # page vanished mid-session
                if parse_graphics(graphics_bytes).status != AcStatus.LIVE:
                    break  # game paused / quit / returned to menu
                packet = parse_physics(physics_bytes).packetId
                if packet == last_packet:
                    stall += 1
                    if stall >= self._stall_polls:
                        break  # physics frozen -> session over
                else:
                    stall = 0
                    last_packet = packet
                    yield (self._clock() - start, physics_bytes, graphics_bytes)
                self._sleep(self._period)
        finally:
            self.close()

    def close(self) -> None:
        if self._own_pages and self._pages is not None:
            self._pages.close()
            self._pages = None


class ACCSource(TelemetrySource):
    """A :class:`TelemetrySource` over any :class:`FrameStream` (live or replay)."""

    native_hz = 333.0

    def __init__(self, stream: FrameStream, *, started_at_utc: str) -> None:
        self._stream = stream
        self._started_at_utc = started_at_utc
        self._static = parse_static(stream.static_bytes())
        # Track length in metres. ACC reports 0 for trackSPlineLength in some
        # states; resolve_track_length falls back to the naming.TRACK_LENGTHS lookup
        # by code so per-frame lap_distance_m is metres, consistent with the session
        # row (static_info uses the same resolver). Stays None for an unknown track,
        # where mapping.lap_distance_m falls back to the normalised 0..1 key.
        self._length = mapping.resolve_track_length(
            self._static.trackSPlineLength, wstr(self._static.track)
        )
        self._iter: Iterator[tuple[float, bytes, bytes]] | None = None
        self._first: tuple[float, bytes, bytes] | None = None

    def _ensure_started(self) -> None:
        """Open the stream iterator and peek its first frame (once)."""
        if self._iter is None:
            self._iter = iter(self._stream)
            self._first = next(self._iter, None)

    def is_running(self) -> bool:
        check = getattr(self._stream, "is_running", None)
        return check() if check is not None else True

    def static_info(self) -> StaticInfo:
        # Session type and air/road temps live on the graphics/physics pages, not
        # the static one, so derive them from the first frame when available.
        self._ensure_started()
        session_type: str | None = None
        air = road = None
        if self._first is not None:
            _, pbytes, gbytes = self._first
            session_type = mapping.session_type_from(parse_graphics(gbytes))
            physics = parse_physics(pbytes)
            air, road = float(physics.airTemp), float(physics.roadTemp)
        return mapping.to_static_info(
            self._static,
            started_at_utc=self._started_at_utc,
            session_type=session_type,
            air_temp_c=air,
            road_temp_c=road,
        )

    def frames(self) -> Iterator[CanonicalFrame]:
        self._ensure_started()
        assert self._iter is not None
        try:
            if self._first is not None:
                yield self._to_frame(self._first)
                self._first = None
            for item in self._iter:
                yield self._to_frame(item)
        finally:
            self._stream.close()

    def _to_frame(self, item: tuple[float, bytes, bytes]) -> CanonicalFrame:
        t, pbytes, gbytes = item
        return mapping.to_frame(
            parse_physics(pbytes),
            parse_graphics(gbytes),
            t=t,
            track_length_m=self._length,
        )
