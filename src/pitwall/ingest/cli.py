"""Ingest ACC telemetry into the local store -- the glue between driving and the
store the MCP server reads.

This is the user-facing command the rest of Phase 1 was missing: without it the
store is only ever populated by tests and ``bench/``, so a fresh install + Claude
Desktop sees an empty database. Three modes:

* ``pitwall-ingest``               -- live ACC ingest, blocking until the session
                                      ends (status leaves LIVE / physics freezes /
                                      page vanishes).
* ``pitwall-ingest session.pwcap`` -- ingest a previously captured recording.
* ``pitwall-ingest --watch``       -- daemon: after a session ends, wait for the
                                      next and auto-ingest it, forever. Start it
                                      once and just drive.

A thin wrapper over the tested spine (:func:`~pitwall.ingest.source.detect_source`
+ :func:`~pitwall.ingest.pipeline.run`); the store setup, the per-lap progress
print, and the human summary are the only new logic. ACC-only imports
(``GameNotRunningError``, ``mapping_exists``) are deferred inside functions --
mirroring :mod:`pitwall.ingest.source` -- so this module imports on any OS:
replay works everywhere; live/watch only do anything on Windows with ACC running.

The watch loop ingests only once an ACC session is actually LIVE (on track), not
merely when the shared-memory pages exist -- those persist in the menus/garage, so
gating on existence spun out empty sessions. The poll takes injectable
``clock``/``sleep``/``is_live`` so the loop is exercised in tests with no game and
no real waiting.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

from pitwall import timeutil
from pitwall.ingest import pipeline
from pitwall.ingest.source import detect_source
from pitwall.storage import db, paths


def _make_watch_progress(log_path: Path | None) -> Callable[[str], None]:
    """A progress sink for the watch daemon: timestamped lines to stdout and,
    if ``log_path`` is set, appended to a log file.

    The daemon usually runs windowless (scheduled task), so the file is how you
    confirm it's alive and see what it captured. A log write must never take the
    daemon down, so file errors are swallowed.
    """

    def emit(msg: str) -> None:
        line = f"[{timeutil.to_iso_utc(timeutil.utc_now())}] {msg}"
        print(line, flush=True)
        if log_path is not None:
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    return emit


def _summarize(conn: sqlite3.Connection, session_id: int) -> str:
    """A one-line summary of what a session ingest produced, from the stored rows."""
    sess = db.get_session(conn, session_id)
    laps = db.laps_for_session(conn, session_id)
    valid = sum(1 for lap in laps if lap.is_valid)
    where = f"{sess.track_name} / {sess.car_name}" if sess else "unknown track/car"
    return f"session {session_id}: {where} -- {len(laps)} lap(s) ingested ({valid} valid)"


def ingest_once(
    conn: sqlite3.Connection,
    *,
    recording_path: str | Path | None,
    progress: Callable[[str], None] | None = print,
    target_hz: float = 50.0,
    min_points: int = 20,
) -> int | None:
    """Resolve a source and ingest one whole session into ``conn``; return its id,
    or ``None`` if the session produced no complete lap (nothing was written).

    Live when ``recording_path`` is None, else replay. Each lap is printed as it
    commits (via ``pipeline.run``'s ``on_lap`` hook) so a live session isn't a
    silent block. Lets ``GameNotRunningError`` / ``FileNotFoundError`` /
    ``ValueError`` propagate -- :func:`main` maps them to exit codes.
    """

    def on_lap(lap_number: int, lap_time_ms: int | None) -> None:
        if progress is None:
            return
        t = f"{lap_time_ms / 1000.0:.3f}s" if lap_time_ms else "--"
        progress(f"  lap {lap_number} ingested  ({t})")

    source = detect_source(recording_path=recording_path)
    return pipeline.run(source, conn, target_hz=target_hz, min_points=min_points, on_lap=on_lap)


def _acc_session_live() -> bool:
    """True only while ACC is in an on-track LIVE session.

    ACC's shared-memory pages exist for the whole time the game *process* runs --
    including in the menus, the garage, and replays -- so page existence is **not**
    a signal that a session has started. The graphics page's ``status`` is: it reads
    ``LIVE`` only when you're actually on track. Gating the watch daemon on this
    (rather than ``mapping_exists``) is what stops it from spinning out empty
    sessions while you sit in the menu. Returns False -- never raises -- when ACC
    isn't running. Windows-only imports are deferred so this module loads anywhere.
    """
    from pitwall.games.acc.shm import GRAPHICS, GameNotRunningError, mapping_exists, open_page, probe_size
    from pitwall.games.acc.structs import AcStatus, parse_graphics

    if not mapping_exists(GRAPHICS):
        return False
    try:
        size = probe_size(GRAPHICS)
        page = open_page(GRAPHICS, size)
        try:
            return parse_graphics(bytes(page[:size])).status == AcStatus.LIVE
        finally:
            page.close()
    except GameNotRunningError:
        return False


def _wait_for_live(
    *,
    is_live: Callable[[], bool],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 2.0,
    progress: Callable[[str], None] | None = print,
    stop_after_s: float | None = None,
) -> bool:
    """Block until ACC is in an on-track LIVE session (``is_live()`` is True).

    Returns True once live, or False if ``stop_after_s`` elapses first (used to
    bound the loop in tests). ``clock``/``sleep``/``is_live`` are injectable so this
    runs offline with no game and no real waiting. Note ``is_live`` checks the
    session *status*, not just shared-memory page existence: the pages also exist in
    the menus/garage, where there is nothing to ingest.
    """
    start = clock()
    announced = False
    while not is_live():
        if stop_after_s is not None and (clock() - start) >= stop_after_s:
            return False
        if progress is not None and not announced:
            progress("waiting for an ACC session -- go on track to start ingesting. (Ctrl-C to stop)")
            announced = True
        sleep(poll_s)
    return True


def run_watch(
    conn: sqlite3.Connection,
    *,
    progress: Callable[[str], None] | None = print,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    max_sessions: int | None = None,
    is_live: Callable[[], bool] | None = None,
) -> int:
    """Loop: wait-for-live -> ingest one session -> repeat. Returns sessions ingested.

    Each iteration is exactly one ``detect_source()`` + ``pipeline.run`` (they are
    one-session-per-call). The loop only ingests once ``is_live()`` reports an
    on-track session (defaults to :func:`_acc_session_live`); gating on session
    *status* rather than page existence is what stops the daemon from busy-looping
    out empty sessions while ACC sits in the menu. A session that produces no lap
    writes nothing and is not counted. ``KeyboardInterrupt`` breaks the loop cleanly
    -- laps from the in-flight session are already committed incrementally. Any
    *other* error in a session is logged and the loop keeps watching, so one bad
    session can't take the daemon down (it would otherwise stop recording silently
    for the rest of the day). ``max_sessions`` bounds the loop in tests; None is
    unbounded (the real daemon).
    """
    live = is_live if is_live is not None else _acc_session_live

    def report(msg: str) -> None:
        if progress is not None:
            progress(msg)

    count = 0
    try:
        while max_sessions is None or count < max_sessions:
            if not _wait_for_live(is_live=live, clock=clock, sleep=sleep, progress=progress):
                break
            report("ACC session detected -- ingesting (drive!).")
            try:
                session_id = ingest_once(conn, recording_path=None, progress=progress)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 -- a daemon must outlive a bad session
                report(f"session ingest failed: {exc!r} -- still watching.")
                sleep(2.0)  # avoid a hot retry loop if the fault is persistent
                continue
            if session_id is None:
                report("session ended with no complete lap -- nothing ingested, still watching.")
                continue
            report(_summarize(conn, session_id))
            count += 1
    except KeyboardInterrupt:
        report("\nstopped -- already-ingested laps are saved.")
    return count


def main(argv: list[str] | None = None) -> int:
    """Console entry point for ``pitwall-ingest`` / ``python -m pitwall.ingest.cli``."""
    parser = argparse.ArgumentParser(
        prog="pitwall-ingest",
        description="Ingest ACC telemetry (live, or a .pwcap recording) into the local store.",
    )
    parser.add_argument(
        "recording",
        nargs="?",
        default=None,
        help="a .pwcap recording to ingest; omit for live ACC ingest",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="after a session ends, wait for the next and auto-ingest it (live only)",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="watch mode: log file path (default: <data dir>/ingest.log)",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="watch mode: don't write a log file (stdout only)",
    )
    args = parser.parse_args(argv)

    # Deferred so this module imports on any platform (replay needs no ACC).
    from pitwall.games.acc.shm import GameNotRunningError

    if args.watch and args.recording is not None:
        print("error: --watch only applies to live ingest, not a recording.", file=sys.stderr)
        return 2
    if args.recording is not None and not Path(args.recording).exists():
        print(f"error: no such file: {args.recording}", file=sys.stderr)
        return 2

    conn = db.connect()
    db.apply_schema(conn)
    try:
        if args.watch:
            log_path = None if args.no_log else Path(args.log) if args.log else paths.data_dir() / "ingest.log"
            emit = _make_watch_progress(log_path)
            emit(f"watching for ACC sessions{'' if log_path is None else f' (log: {log_path})'}.")
            n = run_watch(conn, progress=emit)
            emit(f"done -- ingested {n} session(s).")
            return 0

        if args.recording is None:
            print("Live ingest -- drive now. This ends when the ACC session does. Ctrl-C to stop.")
        else:
            print(f"Ingesting {args.recording} ...")

        try:
            session_id = ingest_once(conn, recording_path=args.recording)
        except KeyboardInterrupt:
            print("\nstopped -- already-ingested laps are saved.", file=sys.stderr)
            return 0
        if session_id is None:
            print("no complete lap was recorded -- were you on track? nothing was ingested.")
            return 0
        print(_summarize(conn, session_id))
        return 0
    except GameNotRunningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        # ValueError covers a corrupt/truncated/bad-magic .pwcap from read_recording.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
