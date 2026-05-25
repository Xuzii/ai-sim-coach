"""The ``.pwcap`` capture container: raw ACC shared-memory pages over time.

A recording is the de-risking artifact for the ACC reader. It stores the raw
bytes of the static page (once) plus a time-stamped stream of physics and
graphics snapshots, so the ctypes structs and canonical mapping can be developed
and regression-tested *offline*, without the game running.

The format is intentionally tiny and dependency-free::

    MAGIC  b"PWCAP1\\n"           7 bytes
    >I     header length         JSON metadata length, big-endian uint32
    bytes  header                UTF-8 JSON: game, page_sizes, started_at_utc, ...
    record* repeated until EOF
      >B   page kind             0=static, 1=physics, 2=graphics
      >Q   ts_micros             microseconds since capture start
      >I   payload length
      bytes payload              the raw page bytes

The static page holds the player's name/nick; scrubbing it requires struct
offsets we don't have yet, so the metadata carries a ``scrubbed`` flag and a
recording stays local until the reader batch can sanitise it for the committed
CI fixture.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO

from pitwall import __version__

MAGIC = b"PWCAP1\n"
_HEADER_LEN = struct.Struct(">I")  # JSON metadata length
_RECORD = struct.Struct(">BQI")  # kind, ts_micros, payload_len


class PageKind(IntEnum):
    STATIC = 0
    PHYSICS = 1
    GRAPHICS = 2


_KIND_KEY = {PageKind.STATIC: "static", PageKind.PHYSICS: "physics", PageKind.GRAPHICS: "graphics"}


@dataclass(frozen=True)
class Record:
    """One captured page snapshot."""

    kind: PageKind
    ts_micros: int
    data: bytes


@dataclass
class Recording:
    """A parsed recording: metadata header plus every captured record in order."""

    meta: dict
    records: list[Record]

    def of_kind(self, kind: PageKind) -> list[Record]:
        """All records of one page kind, in capture order."""
        return [r for r in self.records if r.kind == kind]

    def static(self) -> Record | None:
        """The (single) static-page snapshot, or None if absent."""
        recs = self.of_kind(PageKind.STATIC)
        return recs[0] if recs else None


class RecordingWriter:
    """Streams records to a ``.pwcap`` file. Use as a context manager."""

    def __init__(
        self,
        path: str | Path,
        *,
        page_sizes: dict[str, int],
        poll_hz: float,
        started_at_utc: str,
        game: str = "acc",
        scrubbed: bool = False,
        note: str = "",
    ) -> None:
        self.path = Path(path)
        self.counts: dict[str, int] = {"static": 0, "physics": 0, "graphics": 0}
        self.meta = {
            "format": "pwcap",
            "version": 1,
            "game": game,
            "page_sizes": dict(page_sizes),
            "started_at_utc": started_at_utc,
            "poll_hz": poll_hz,
            "pitwall_version": __version__,
            "scrubbed": scrubbed,
            "note": note,
        }
        self._fh: BinaryIO = self.path.open("wb")
        payload = json.dumps(self.meta, separators=(",", ":")).encode("utf-8")
        self._fh.write(MAGIC)
        self._fh.write(_HEADER_LEN.pack(len(payload)))
        self._fh.write(payload)

    def _write(self, kind: PageKind, ts_micros: int, data: bytes) -> None:
        self._fh.write(_RECORD.pack(int(kind), ts_micros, len(data)))
        self._fh.write(data)
        self.counts[_KIND_KEY[kind]] += 1

    def write_static(self, ts_micros: int, data: bytes) -> None:
        self._write(PageKind.STATIC, ts_micros, data)

    def write_physics(self, ts_micros: int, data: bytes) -> None:
        self._write(PageKind.PHYSICS, ts_micros, data)

    def write_graphics(self, ts_micros: int, data: bytes) -> None:
        self._write(PageKind.GRAPHICS, ts_micros, data)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> RecordingWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _records(raw: bytes, pos: int) -> Iterator[Record]:
    n = len(raw)
    while pos < n:
        if pos + _RECORD.size > n:
            raise ValueError("truncated recording: incomplete record header")
        kind, ts_micros, plen = _RECORD.unpack_from(raw, pos)
        pos += _RECORD.size
        if pos + plen > n:
            raise ValueError("truncated recording: incomplete record payload")
        yield Record(PageKind(kind), ts_micros, raw[pos : pos + plen])
        pos += plen


def read_recording(path: str | Path) -> Recording:
    """Parse a ``.pwcap`` file. Raises :class:`ValueError` on a corrupt/truncated file."""
    raw = Path(path).read_bytes()
    if raw[: len(MAGIC)] != MAGIC:
        raise ValueError("not a pwcap recording (bad magic)")
    pos = len(MAGIC)
    if pos + _HEADER_LEN.size > len(raw):
        raise ValueError("truncated recording: missing header length")
    (hlen,) = _HEADER_LEN.unpack_from(raw, pos)
    pos += _HEADER_LEN.size
    if pos + hlen > len(raw):
        raise ValueError("truncated recording: incomplete header")
    meta = json.loads(raw[pos : pos + hlen].decode("utf-8"))
    pos += hlen
    return Recording(meta=meta, records=list(_records(raw, pos)))
