# Verifying Phase 1 — the whole pipeline, end to end

This is the runbook for answering one question: **does pitwall actually work, from a
car on track to Claude answering a question about your lap?** It's split into three
tracks by how much you need running:

- **Track A — Offline (no game):** prove the code, storage, query layer, and packaging.
  Anyone can run this anytime. *(All of this is currently passing — see `STATUS.md`.)*
- **Track B — Live capture (needs ACC on the same machine):** prove real telemetry
  flows from the game into the store.
- **Track C — The real product (Claude Desktop):** prove Claude answers the five
  success-criteria questions over the MCP server.

Phase 1 is "done and working as intended" when all three pass — **all three passed as of
2026-05-25.** Track A passes on every run; Track B (live capture → store) is a single
command (`pitwall-ingest`) and was exercised; Track C (Claude Desktop answering the five
questions) was completed and recorded (the demo is `docs/demo.gif`). The steps below
remain as the repeatable runbook.

All commands are PowerShell from the repo root. The dev venv is `.venv`. The live data
store lives at **`%LOCALAPPDATA%\pitwall`** (`pitwall.db` + `traces\` + `exports\`);
set `PITWALL_DATA_DIR` to point it somewhere else (the tests do this so they never
touch your real store). The store runs in **WAL** mode, so you'll also see
`pitwall.db-wal` / `pitwall.db-shm` sidecars there — that's expected, and it lets Claude
read the store while `pitwall-ingest` writes to it.

---

## Track A — Offline verification (no game required)

### A1. Install (editable, with dev deps)

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,bench]"
```

### A2. Lint

```powershell
.\.venv\Scripts\ruff.exe check .
```
Expect: `All checks passed!`

### A3. Tests (the CI safety net — includes the ACC reader, no game needed)

```powershell
.\.venv\Scripts\python.exe -m pytest
```
Expect: **`100 passed`**. This replays the scrubbed fixture
`tests/fixtures/acc-nurburgring-slim.pwcap` through the real pipeline, so the ACC
struct parsing, canonical mapping, lap/sector segmentation, storage, all 8 tools, and
`pitwall-ingest`'s replay/live/`--watch` paths are exercised without ACC running.

### A4. Build + metadata check (what PyPI will see)

```powershell
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\twine.exe check dist/*
```
Expect: a `pitwall_mcp-0.1.0` wheel + sdist in `dist\`, and `twine check` → `PASSED`.

### A5. End-to-end on real data — the 5 success-criteria queries

This ingests a real `.pwcap` into an **isolated** store and runs the five plan queries
through the actual query functions Claude's tools call:

```powershell
.\.venv\Scripts\python.exe bench/verify_success_criteria.py
```
Expect: Q1 fastest valid lap **2:04.126**; Q2 brake/speed summary for the first braking
zone; Q3 a graceful "needs two valid laps" note (this short capture has one clean lap);
Q4 per-wheel tyre temps across the stint; Q5 a CSV path on disk. If `acc-spa.pwcap`
isn't present it falls back to the slim fixture.

### A6. Performance numbers (optional)

```powershell
.\.venv\Scripts\python.exe bench/run.py
```
Writes `bench/results.json` and prints ingest throughput, storage/lap, per-tool latency,
the token/detail-level curve, and the stdio cold-start. Cross-check against
`BENCHMARKS.md`.

**Track A passing means:** the pipeline, storage, query layer, tool responses, and the
package are all correct. The only things it does *not* prove are live capture from the
game and the literal Claude round-trip — Tracks B and C.

---

## Track B — Live capture (ACC running on this machine)

ACC's Shared Memory only exists while ACC runs on the same PC. You did this once already
during the M2 drive-test; this is how to repeat it from scratch.

### B1. Launch ACC and get on track

Start a Practice session and get the car moving (the pages only populate in-session).

### B2. Capture raw pages to a recording

In a second terminal, **run exactly one** capture process (two concurrent captures
corrupt the file):

```powershell
.\.venv\Scripts\pitwall-capture.exe --out my-session.pwcap --duration 120
```
Drive ~2–3 clean laps **plus one deliberate off-track** (so you can confirm invalidation
later). If ACC isn't running you get a clear `GameNotRunningError`, not a crash.

### B3. Sanity-check the decode

```powershell
.\.venv\Scripts\pitwall-inspect.exe my-session.pwcap
```
Confirm: correct track + car, sane throttle/brake/speed/gear samples, a `completedLaps`
progression that increments, `packetId monotonic = True`, and a believable speed range.
If these look right, the struct layout decoded your session correctly.

### B4. Ingest the recording into the store

One command writes the recording into your **real** store (`%LOCALAPPDATA%\pitwall`):

```powershell
.\.venv\Scripts\pitwall-ingest.exe my-session.pwcap
```
Expect a per-lap progress line as each lap lands, then a summary
(`session N: <track> / <car> -- M lap(s) ingested (K valid)`). To keep your real store
clean while testing, set an isolated dir first:
`$env:PITWALL_DATA_DIR="$PWD\_scratch_store"`.

To skip the capture-then-ingest two-step entirely and ingest **live as you drive**, run
`pitwall-ingest` with no path (it ends when the ACC session does), or
`pitwall-ingest --watch` to leave it running and auto-ingest every session back-to-back.
`--watch` survives a bad session (logs it and keeps watching) and writes
`%LOCALAPPDATA%\pitwall\ingest.log` (override `--log`, disable `--no-log`).

> **Optional — auto-start the watcher at logon (dormant until ACC):** register a
> windowless logon task so you never have to start it manually. It costs ~nothing while
> ACC is closed (one shared-memory check every 2 s) and only ingests when you're on track.
> Create / remove:
> ```powershell
> $py = "$PWD\.venv\Scripts\pythonw.exe"
> $act = New-ScheduledTaskAction -Execute $py -Argument '-m pitwall.ingest.cli --watch' -WorkingDirectory $PWD
> $trg = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
> $pri = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
> $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
> Register-ScheduledTask -TaskName 'pitwall-ingest-watch' -Action $act -Trigger $trg -Principal $pri -Settings $set -Force
> # remove:  Unregister-ScheduledTask -TaskName 'pitwall-ingest-watch' -Confirm:$false
> ```
> **Don't also run `pitwall-ingest --watch` by hand while the task is active** — two live
> watchers would both ingest and create duplicate sessions. Stop the task first
> (`Stop-ScheduledTask -TaskName 'pitwall-ingest-watch'`) if you want to run it manually.

### B5. Confirm laps landed

```powershell
.\.venv\Scripts\python.exe -c "from pitwall.storage import db; from pitwall.tools import queries; c=db.connect(); db.apply_schema(c); import json; print(json.dumps(queries.list_sessions(c), indent=2, default=str)); c.close()"
```
Expect your session, with the right track/car and lap count. Then `list_laps` should
show your off-track lap as `is_valid: false`.

**Track B passing means:** real telemetry flows game → store. (A live capture can also
be turned into a permanent CI fixture with `pitwall-scrub` to strip your player name —
that's how `tests/fixtures/acc-nurburgring-slim.pwcap` was made.)

---

## Track C — The real product (Claude Desktop round-trip)

This is the last unproven hop and the basis for the demo GIF.

### C1. Install pitwall as a normal user would

From PyPI once published (`pip install pitwall-mcp`), or test the built wheel locally:

```powershell
pip install dist\pitwall_mcp-0.1.0-py3-none-any.whl
```

### C2. Register it with Claude Desktop

Add to `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pitwall": { "command": "pitwall" }
  }
}
```
Restart Claude Desktop. It should list pitwall's 8 tools (the cold start to first tool
call is ~1 s — see `BENCHMARKS.md §5`).

### C3. Make sure the store has data

Claude reads `%LOCALAPPDATA%\pitwall`. Populate it via Track B (step B4), or for a quick
demo, ingest a recording into the real store with `pitwall-ingest acc-spa.pwcap`
(**without** the `PITWALL_DATA_DIR` override).

### C4. Ask the five success-criteria questions

In Claude Desktop:
1. "What was my fastest lap at the Nürburgring this week?"
2. "What was my brake pressure into the first braking zone on that lap?"
3. "Compare my fastest and second-fastest lap — where did I lose time?"
4. "What were my tyre temps over the stint?"
5. "Export that lap to CSV."

Confirm Claude routes each to the right tool and the answers match what
`bench/verify_success_criteria.py` produced (e.g. fastest valid lap 2:04.126). Record
this as the demo GIF.

**Track C passing means Phase 1 works as advertised.** Capture the GIF here, then do the
account-gated ship steps in `STATUS.md`.

---

## Quick reference — what proves what

| If you want to confirm… | Run |
|---|---|
| The code is correct (incl. ACC reader) | Track A1–A3 (`pytest`, `ruff`) |
| The package will install from PyPI | Track A4 (`build` + `twine check`) |
| The query layer answers the 5 questions on real data | Track A5 |
| Performance matches the published numbers | Track A6 |
| Real telemetry flows game → store | Track B |
| Claude answers over MCP (the real product) | Track C |
