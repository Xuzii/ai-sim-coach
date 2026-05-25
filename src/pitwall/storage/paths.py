"""Filesystem locations for the local data store.

Defaults to ``%LOCALAPPDATA%/pitwall`` on Windows and the XDG data dir elsewhere.
Set ``PITWALL_DATA_DIR`` to override -- the test suite points it at a tmp dir so
tests never touch the real store.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_OVERRIDE = "PITWALL_DATA_DIR"


def data_dir() -> Path:
    """Root directory for the SQLite database, traces, and exports (created on demand)."""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        base = Path(override)
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        base = Path(local) / "pitwall"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = (Path(xdg) if xdg else Path.home() / ".local" / "share") / "pitwall"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    """Path to the SQLite metadata database."""
    return data_dir() / "pitwall.db"


def traces_dir() -> Path:
    """Directory holding one Parquet file per lap (created on demand)."""
    d = data_dir() / "traces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def exports_dir() -> Path:
    """Directory for CSV exports handed back to the user (created on demand)."""
    d = data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d
