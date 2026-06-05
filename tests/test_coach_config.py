"""P2-C1: CoachConfig defaults + load_dotenv + resolve_api_key."""

from __future__ import annotations

import os

import pytest

from pitwall.coach.config import CoachConfig, load_dotenv, resolve_api_key


def test_coach_config_defaults():
    cfg = CoachConfig()
    assert cfg.provider == "gemini"
    assert cfg.model == "gemini-2.5-flash"
    assert cfg.api_key_env == "GEMINI_API_KEY"
    assert cfg.max_iterations == 12
    assert 0.0 <= cfg.temperature <= 1.0
    assert cfg.reference_lap_id is None
    assert cfg.system_prompt is None


def test_load_dotenv_populates_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("PITWALL_TEST_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("PITWALL_TEST_KEY=from-file\n", encoding="utf-8")
    load_dotenv(env)
    assert os.environ["PITWALL_TEST_KEY"] == "from-file"


def test_load_dotenv_does_not_override_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("PITWALL_TEST_KEY", "from-shell")
    env = tmp_path / ".env"
    env.write_text("PITWALL_TEST_KEY=from-file\n", encoding="utf-8")
    load_dotenv(env)
    assert os.environ["PITWALL_TEST_KEY"] == "from-shell"


def test_load_dotenv_skips_comments_blank_lines_and_strips_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("PITWALL_QUOTED", raising=False)
    monkeypatch.delenv("PITWALL_BARE", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        'PITWALL_QUOTED="quoted value"\n'
        "PITWALL_BARE=bare\n"
        "malformed-line-with-no-equals\n",
        encoding="utf-8",
    )
    load_dotenv(env)
    assert os.environ["PITWALL_QUOTED"] == "quoted value"
    assert os.environ["PITWALL_BARE"] == "bare"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    load_dotenv(tmp_path / "does-not-exist.env")  # must not raise


def test_resolve_api_key_returns_value(monkeypatch):
    monkeypatch.setenv("PITWALL_RESOLVE_TEST", "abc-123")
    cfg = CoachConfig(api_key_env="PITWALL_RESOLVE_TEST")
    assert resolve_api_key(cfg) == "abc-123"


def test_resolve_api_key_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env at this CWD
    monkeypatch.delenv("PITWALL_RESOLVE_TEST", raising=False)
    cfg = CoachConfig(api_key_env="PITWALL_RESOLVE_TEST")
    with pytest.raises(RuntimeError, match="PITWALL_RESOLVE_TEST"):
        resolve_api_key(cfg)
