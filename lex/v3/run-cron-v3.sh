#!/usr/bin/env bash
# run-cron-v3.sh — NeuralTape v3 cron entry point
#
# Called by systemd timer every 5 minutes.
# Iterates over recent transcripts (not just the latest) so that closed
# sessions left behind by the previous "max(mtime)" logic get backfilled.
# Idempotency is enforced inside run_once via growth-aware markers.

set -euo pipefail

NEURAL_ROOT="/run/media/gcaponi/Back-Up/NeuralTape"
export NEURALTAPE_V3=1

if [ -f "$NEURAL_ROOT/.env" ]; then
    set -a
    . "$NEURAL_ROOT/.env"
    set +a
fi

cd "$NEURAL_ROOT"

/usr/bin/python3 << 'PYEOF'
import json, os, subprocess, sys, time
from pathlib import Path

tape_root = Path.cwd()

# Tunables — kept conservative to bound LLM cost per cron tick.
MAX_AGE_DAYS = 7           # only consider transcripts touched in the last week
MIN_BYTES = 5120           # skip tiny transcripts (<5KB, likely session stub)
ACTIVE_THRESHOLD_SEC = 600 # skip transcripts modified in the last 10 min (active session)
MAX_RUNS_PER_TICK = 3      # hard cap on LLM calls per cron invocation

# Resolve project PER transcript via harvest_sessions heuristic.
# Each transcript may belong to a different project (workspace folder), so we
# cannot reuse a single resolution across all candidates.
plan_by_session: dict[str, dict] = {}
try:
    result = subprocess.run(
        [sys.executable, "tools/harvest_sessions.py", "--limit", "50"],
        capture_output=True, text=True, cwd=tape_root,
    )
    plan = json.loads(result.stdout)
    for entry in plan:
        sid = entry.get("session_id")
        if sid and entry.get("project_id") and entry.get("project_root"):
            plan_by_session[sid] = entry
except (json.JSONDecodeError, IndexError, subprocess.SubprocessError):
    pass  # fall back to tape_root below

# Collect candidate transcripts across all VS Code workspace folders.
candidates = []
now = time.time()
for ws in Path.home().glob(".config/Code/User/workspaceStorage/*"):
    tdir = ws / "GitHub.copilot-chat" / "transcripts"
    if not tdir.is_dir():
        continue
    for tp in tdir.glob("*.jsonl"):
        try:
            st = tp.stat()
        except OSError:
            continue
        age_days = (now - st.st_mtime) / 86400
        if age_days > MAX_AGE_DAYS:
            continue
        if st.st_size < MIN_BYTES:
            continue
        if (now - st.st_mtime) < ACTIVE_THRESHOLD_SEC:
            continue  # session is live, defer until idle
        candidates.append((st.st_mtime, st.st_size, tp))

if not candidates:
    print("[v3-cron] no candidate transcripts (age/size/active filters)")
    sys.exit(0)

# Process newest first, capped at MAX_RUNS_PER_TICK per tick.
# Idempotency inside run_once means already-classified sessions return
# skipped=True with eps=0 and cost no LLM call.
candidates.sort(reverse=True)
candidates = candidates[:MAX_RUNS_PER_TICK]

print(f"[v3-cron] {len(candidates)} candidate(s)")

sys.path.insert(0, str(tape_root))
os.environ["NEURALTAPE_V3"] = "1"
from lex.v3.run import run_once

total_eps = 0
processed = 0
for mtime, size, tp in candidates:
    sid = tp.stem
    plan_entry = plan_by_session.get(sid) or {}
    project_root = Path(plan_entry.get("project_root", tape_root))
    project_id_hint = plan_entry.get("project_id", "<unknown>")
    try:
        res = run_once(
            transcript_path=tp,
            project_root=project_root,
            tape_root=tape_root,
        )
    except Exception as exc:  # never let one bad transcript kill the tick
        print(f"[v3-cron] {sid}: ERROR {exc}")
        continue
    total_eps += res.episodes_written
    if not res.skipped:
        processed += 1
    print(
        f"[v3-cron] {sid}: project={project_id_hint} skipped={res.skipped} "
        f"eps={res.episodes_written} ({res.duration_seconds:.1f}s)"
    )

print(f"[v3-cron] tick done: processed={processed} eps_total={total_eps}")
PYEOF
