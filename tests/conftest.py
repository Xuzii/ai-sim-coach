"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the data store at an isolated tmp dir for the duration of a test."""
    monkeypatch.setenv("PITWALL_DATA_DIR", str(tmp_path))
    return tmp_path
