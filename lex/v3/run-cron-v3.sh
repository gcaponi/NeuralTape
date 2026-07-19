#!/usr/bin/env bash
# run-cron-v3.sh — NeuralTape v3 cron entry point
#
# Called by systemd timer every 5 minutes.
# Uses Python to find latest transcript + resolve project + run.
# Idempotent: run_once skips already-classified sessions.

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

# Find latest transcript
transcripts = list(Path.home().glob(
    ".config/Code/User/workspaceStorage/*/GitHub.copilot-chat/transcripts/*.jsonl"
))
if not transcripts:
    print("[v3-cron] no transcripts found")
    sys.exit(0)

latest = max(transcripts, key=lambda p: p.stat().st_mtime)
session_id = latest.stem

# Resolve project via harvest heuristic
result = subprocess.run(
    [sys.executable, "tools/harvest_sessions.py", "--limit", "1"],
    capture_output=True, text=True, cwd=tape_root,
)
try:
    plan = json.loads(result.stdout)
    if plan and plan[0].get("project_id") and plan[0].get("project_root"):
        project_id = plan[0]["project_id"]
        project_root = Path(plan[0]["project_root"])
    else:
        project_root = tape_root
except (json.JSONDecodeError, IndexError):
    project_root = tape_root

print(f"[v3-cron] session={session_id} → {project_root.name}")

# Run v3
sys.path.insert(0, str(tape_root))
os.environ["NEURALTAPE_V3"] = "1"

from lex.v3.run import run_once
res = run_once(
    transcript_path=latest,
    project_root=project_root,
    tape_root=tape_root,
)
print(f"[v3-cron] done: skipped={res.skipped} eps={res.episodes_written} ({res.duration_seconds:.1f}s)")
PYEOF
