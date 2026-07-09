"""``.ibt`` file decode -- ported from the visualiser's ``rtv/ingest/ibt.py``.

``read_ibt_session_info``, ``_window_columns``, ``_ids_from_info`` and the
``_str_or_none`` / ``_int_or_none`` helpers are kept **near-verbatim** from the visualiser
so the binary decode matches byte-for-byte (the visualiser logger is swapped for the
stdlib one). ``open_ibt`` and ``iter_samples`` are thin pitwall additions: ``open_ibt``
lazy-imports ``irsdk`` (the optional ``iracing`` extra) and ``iter_samples`` streams the
visualiser's chunked-window read as one ``{var: value}`` dict per record, so memory stays
bounded on a multi-hundred-MB file.

We intentionally do **not** port the visualiser's ``import_ibt`` / ``Catalog`` /
``FrameBuffer`` -- pitwall has its own canonical schema and Parquet writer; the adapter in
:mod:`reader` is the seam.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pitwall.games.iracing.ibt_types import IRType, mapping_for

log = logging.getLogger("pitwall.games.iracing.ibt")


class IRacingSDKMissingError(RuntimeError):
    """Raised when ``pyirsdk`` (the ``iracing`` extra) is not installed."""


def open_ibt(path: str | Path):
    """Open a ``.ibt`` file with ``pyirsdk`` and return the opened ``IBT`` object.

    Lazy-imports ``irsdk`` so the base install stays lean (mirrors how the coach defers
    ``google-genai``). Raises :class:`IRacingSDKMissingError` with an actionable message
    when the ``iracing`` extra is absent.
    """
    try:
        import irsdk
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise IRacingSDKMissingError(
            "iRacing .ibt parsing needs the 'iracing' extra -- install it with "
            'pip install -e ".[iracing]"  (provides pyirsdk + pyyaml).'
        ) from exc

    ibt = irsdk.IBT()
    ibt.open(str(path))
    return ibt


def read_ibt_session_info(ibt) -> dict[str, Any]:
    """Read and parse the session-info YAML embedded in an .ibt file.

    The pyirsdk IBT class does not parse session info, but the bytes live in the
    file header. We read them directly and parse leniently.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise IRacingSDKMissingError(
            "iRacing .ibt parsing needs PyYAML (the 'iracing' extra) -- install it with "
            'pip install -e ".[iracing]".'
        ) from exc
    try:
        header = ibt._header
        mem = ibt._shared_mem
        offset = header.session_info_offset
        length = header.session_info_len
        raw = bytes(mem[offset : offset + length])
        text = raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace")
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not parse .ibt session info: %s", exc)
        return {}


def _window_columns(ibt, names: list[str], start: int, count: int) -> dict[str, list]:
    """Read `count` records starting at `start` for the given variables.

    Mirrors IBT.get_all but bounded to a record window, using a single
    struct.unpack_from per (variable, record) over the mmap.
    """
    header = ibt._header
    mem = ibt._shared_mem
    buf_offset = header.var_buf[0].buf_offset
    buf_len = header.buf_len
    headers = ibt._var_headers_dict

    out: dict[str, list] = {}
    for name in names:
        vh = headers[name]
        fmt = mapping_for(IRType(vh.type)).struct_char * vh.count
        is_array = vh.count > 1
        base = vh.offset + buf_offset
        col: list[Any] = []
        for i in range(start, start + count):
            res = struct.unpack_from(fmt, mem, base + i * buf_len)
            col.append(list(res) if is_array else res[0])
        out[name] = col
    return out


def iter_samples(ibt, names: list[str], *, chunk_rows: int = 10_000) -> Iterator[dict[str, Any]]:
    """Yield one ``{name: value}`` dict per record, over the whole file.

    Reads in tick-window chunks straight off the mmap (the visualiser's ``import_ibt``
    chunk loop) so memory stays bounded regardless of file size. ``names`` should list
    only the variables the caller needs -- absent names are silently skipped, so a build
    that lacks a given channel doesn't break the read.
    """
    headers = ibt._var_headers_dict
    present = [n for n in names if n in headers]
    total = int(ibt._disk_header.session_record_count)
    for start in range(0, total, chunk_rows):
        count = min(chunk_rows, total - start)
        cols = _window_columns(ibt, present, start, count)
        for r in range(count):
            yield {name: cols[name][r] for name in present}


def _ids_from_info(info: dict[str, Any]):
    """Extract car/track identifiers from a parsed session-info dict."""
    weekend = info.get("WeekendInfo", {}) if isinstance(info, dict) else {}
    track_id = _str_or_none(weekend.get("TrackID"))
    track_name = weekend.get("TrackDisplayName") or weekend.get("TrackName")
    ir_sid = _int_or_none(weekend.get("SessionID"))
    ir_subsid = _int_or_none(weekend.get("SubSessionID"))

    car_id = None
    driver_info = info.get("DriverInfo", {}) if isinstance(info, dict) else {}
    drivers = driver_info.get("Drivers")
    pcar_idx = driver_info.get("DriverCarIdx")
    if isinstance(drivers, list) and isinstance(pcar_idx, int):
        for d in drivers:
            if d.get("CarIdx") == pcar_idx:
                car_id = _str_or_none(d.get("CarID")) or d.get("CarScreenName")
                break
    return car_id, track_id, str(track_name) if track_name else None, ir_sid, ir_subsid


def _str_or_none(v: Any) -> str | None:
    return str(v) if v is not None else None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
