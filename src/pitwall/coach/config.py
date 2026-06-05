"""Runtime configuration for the coach + a tiny ``.env`` loader.

:class:`CoachConfig` carries the provider/model/iteration knobs that the C2 CLI
will fill from flags + env. ``resolve_api_key`` is the single place we read the
key from ``os.environ``, after first folding in any ``.env`` file at the project
root so a freshly-spawned shell still has it. We do not depend on
``python-dotenv``: the parser is ~15 lines and the loader skips lines already
set in ``os.environ`` so a shell override always wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CoachConfig:
    """Knobs for one ``analyze_lap`` run.

    Defaults target Google Gemini's free tier; ``provider`` + ``model`` +
    ``api_key_env`` let a future ``ClaudeClient`` drop in with no other code
    change. ``reference_lap_id=None`` means "use the fastest valid lap in the
    same session" (matches ``get_sector_analysis``'s default).
    """

    provider: str = "gemini"
    model: str = "gemini-2.5-flash"
    api_key_env: str = "GEMINI_API_KEY"
    max_iterations: int = 12
    temperature: float = 0.2
    reference_lap_id: int | None = None
    system_prompt: str | None = None


def _find_dotenv(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a ``.env`` file. Returns None if absent.

    Useful when the CWD is a sub-directory (tests, ``bench/``) -- we still find
    the repo-root ``.env``."""
    for parent in (start, *start.parents):
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(path: Path | None = None) -> None:
    """Read ``KEY=value`` pairs from a ``.env`` file into ``os.environ``.

    Values already present in ``os.environ`` are **not** overridden, so a
    shell-set var (e.g. ``$env:GEMINI_API_KEY``) always wins over the file.
    Silent no-op when no file is found -- callers don't have to guard for it.

    Lines starting with ``#`` and blank lines are skipped; surrounding single
    or double quotes on the value are stripped (``KEY="abc"`` -> ``abc``).
    Malformed lines (no ``=``) are skipped silently rather than crashing the
    agent.
    """
    target = path if path is not None else _find_dotenv(Path.cwd())
    if target is None or not target.is_file():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_api_key(config: CoachConfig) -> str:
    """Look up the LLM API key for ``config.api_key_env``.

    Loads ``.env`` first (without overriding the shell), then reads
    ``os.environ``. Raises :class:`RuntimeError` with a pointer to both the env
    var and ``.env.example`` if the key is still missing -- same graceful-error
    style as the "game not running" path on the ACC reader."""
    load_dotenv()
    value = os.environ.get(config.api_key_env, "").strip()
    if not value:
        raise RuntimeError(
            f"{config.api_key_env} is not set. Add it to .env at the project root "
            f"(see .env.example) or run `$env:{config.api_key_env}=\"...\"` in your shell."
        )
    return value
