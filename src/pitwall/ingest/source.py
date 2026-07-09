"""The telemetry source contract.

Any game reader implements :class:`TelemetrySource`: a one-shot ``static_info()``
and a ``frames()`` iterator of :class:`~pitwall.ingest.frames.CanonicalFrame`.
The synthetic source (this chunk) and the live ACC reader (C3) both satisfy it,
so the downsample/segment/persist pipeline never branches on game.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from pitwall.ingest.frames import CanonicalFrame, StaticInfo


class TelemetrySource(ABC):
    #: Native sample rate of the source in Hz (ACC physics ~333, iRacing ~60).
    native_hz: float = 60.0

    @abstractmethod
    def static_info(self) -> StaticInfo:
        """Return the once-per-session context (track, car, length, sector count)."""

    @abstractmethod
    def frames(self) -> Iterator[CanonicalFrame]:
        """Yield canonical frames in time order until the session ends."""


def detect_source(
    *,
    recording_path: str | Path | None = None,
    started_at_utc: str | None = None,
    game: str = "acc",
) -> TelemetrySource:
    """Return the telemetry source to ingest from.

    With ``recording_path`` set, replays a captured ACC ``.pwcap`` or an iRacing
    ``.ibt`` offline. Otherwise opens live ACC, raising
    :class:`~pitwall.games.acc.shm.GameNotRunningError` when the game isn't running.
    iRacing supports ``.ibt`` replay (``game="iracing"``); live iRacing is a follow-up.

    Game imports are deferred inside this function so the module stays importable
    on non-Windows boxes and the synthetic-only pipeline never pulls in ctypes or
    the optional ``iracing`` extra.
    """
    if game == "iracing":
        if recording_path is None:
            raise NotImplementedError(
                "live iRacing ingest is a follow-up; pass a .ibt file to replay."
            )
        from pitwall.games.iracing.reader import IRacingSource

        return IRacingSource.from_ibt_file(recording_path, started_at_utc=started_at_utc)

    if game != "acc":
        raise NotImplementedError(f"unsupported game {game!r}; expected 'acc' or 'iracing'.")

    from pitwall.games.acc.reader import ACCSource, LiveFrameStream, RecordingFrameStream
    from pitwall.games.acc.recording import read_recording

    if recording_path is not None:
        recording = read_recording(recording_path)
        started = started_at_utc or recording.meta.get("started_at_utc") or _now_utc()
        return ACCSource(RecordingFrameStream(recording), started_at_utc=started)

    from pitwall.games.acc.shm import PHYSICS, GameNotRunningError, LivePages, mapping_exists

    if not mapping_exists(PHYSICS):
        raise GameNotRunningError(
            "ACC is not running -- launch ACC and enter a session first."
        )
    return ACCSource(LiveFrameStream(LivePages()), started_at_utc=started_at_utc or _now_utc())


def _now_utc() -> str:
    from pitwall import timeutil

    return timeutil.to_iso_utc(timeutil.utc_now())
