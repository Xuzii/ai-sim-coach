"""Per-lap telemetry traces as columnar Parquet.

One file per lap, columns in canonical schema order, sorted ascending by
``lap_distance_m``. Reads project only the requested channels and can slice a
distance range without materialising the whole lap -- the reason traces are
Parquet, not SQLite blobs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from pitwall import channels
from pitwall.storage import paths


def lap_trace_relpath(session_id: int, lap_number: int) -> str:
    """Path of a lap's Parquet file, relative to the traces directory."""
    return f"s{session_id}/lap_{lap_number:03d}.parquet"


def resolve_trace_path(relpath: str | Path) -> Path:
    """Resolve a stored relative trace path against the configured traces directory."""
    return paths.traces_dir() / relpath

_ARROW_TYPES = {"float32": pa.float32(), "int32": pa.int32(), "int8": pa.int8()}


def _arrow_type(dtype: str) -> pa.DataType:
    return _ARROW_TYPES[dtype]


def build_table(data: Mapping[str, Sequence]) -> pa.Table:
    """Build a canonical-schema Arrow table from a column->values mapping.

    Every trace column is present in canonical order; a column absent from
    ``data`` is filled with nulls so every lap file shares one schema. The
    distance key must be provided.
    """
    if channels.DISTANCE_KEY not in data:
        raise KeyError(f"trace data missing required column {channels.DISTANCE_KEY!r}")
    n = len(data[channels.DISTANCE_KEY])
    arrays, names = [], []
    for name in channels.trace_columns():
        values = data.get(name)
        if values is None:
            values = [None] * n
        arrays.append(pa.array(values, type=_arrow_type(channels.dtype_for(name))))
        names.append(name)
    return pa.Table.from_arrays(arrays, names=names)


def write_lap_trace(path: str | Path, data: Mapping[str, Sequence]) -> int:
    """Write a lap trace to ``path`` (zstd). Returns the number of rows written."""
    table = build_table(data)
    pq.write_table(table, str(path), compression="zstd", compression_level=3)
    return table.num_rows


def read_lap_trace(
    path: str | Path,
    columns: Sequence[str] | None = None,
    distance_range: tuple[float, float] | None = None,
) -> pa.Table:
    """Read a lap trace, optionally projecting columns and slicing a distance range.

    ``columns`` lists the channels you want; the distance key is always included
    so the result stays interpretable. ``distance_range`` is an inclusive
    ``(min_m, max_m)`` filter on ``lap_distance_m``.
    """
    read_cols: list[str] | None = None
    if columns is not None:
        read_cols = list(dict.fromkeys([channels.DISTANCE_KEY, *columns]))
    table = pq.read_table(str(path), columns=read_cols)
    if distance_range is not None:
        lo, hi = distance_range
        dist = table.column(channels.DISTANCE_KEY)
        mask = pc.and_(pc.greater_equal(dist, lo), pc.less_equal(dist, hi))
        table = table.filter(mask)
    return table


def to_columns(table: pa.Table) -> dict[str, list]:
    """Convert an Arrow table to a plain ``{column: [values]}`` dict for JSON-ish use."""
    return {name: table.column(name).to_pylist() for name in table.column_names}
