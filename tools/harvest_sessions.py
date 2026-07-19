#!/usr/bin/env python3
"""harvest_sessions — list VS Code transcripts and assign a v3 project_id.

Deterministic path-based heuristic. NO LLM cost. Used for the v3 exit-criteria
validation campaign (10 sessions on >=2 projects).

Strategy (priority order):
    1. Parse each transcript JSONL and count occurrences of known project roots
       in the message content (file paths, commands). The project with the most
       distinct path references wins.
    2. Tie-break: the project whose root appears in the highest-numbered turn
       (most recent activity).
    3. Fallback: 'unknown' if no project root is mentioned.

Project roots come from the .neuraltape/project.yaml files under DEFAULT_BASE.

Output: a JSON plan consumable by validate_sessions.py:
    [
      {"session_id": "...", "transcript_path": "...", "project_id": "etercervo",
       "project_root": "/run/media/.../EterCervo", "score": {project: count},
       "bytes": 123456, "mtime_iso": "2026-07-18..."},
      ...
    ]

Usage:
    python tools/harvest_sessions.py --min-bytes 50000 > validation-plan.json
    python tools/harvest_sessions.py --limit 10 --pretty
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Same default base as bootstrap_projects.py.
DEFAULT_BASE = Path("/run/media/gcaponi/Back-Up")
DEFAULT_TRANSCRIPTS_GLOB = "~/.config/Code/User/workspaceStorage/*/GitHub.copilot-chat/transcripts/*.jsonl"

# Roots that are NOT project workspaces (exclude from heuristic).
EXCLUDE_DIRS = {".venv", "node_modules", "__pycache__", ".git"}


def discover_projects(base: Path) -> dict[str, Path]:
    """Find every workspace root that has a .neuraltape/project.yaml.

    Returns {project_id: root_path}.
    """
    projects: dict[str, Path] = {}
    for cfg in sorted(base.glob("*/.neuraltape/project.yaml")):
        try:
            import yaml
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            pid = str(data.get("project_id", "")).strip()
            if pid and re.match(r"^[a-z0-9][a-z0-9-]{0,31}$", pid):
                projects[pid] = cfg.parent.parent.resolve()
        except Exception as e:
            print(f"[warn] cannot read {cfg}: {e}", file=sys.stderr)
    return projects


def extract_text_from_transcript(path: Path, max_bytes: int = 4_000_000) -> str:
    """Read transcript JSONL and concatenate all message texts.

    VS Code Copilot transcript schema (verified 2026-07-18):
        {"type": "user.message" | "assistant.message" | ...,
         "data": {"content": "...", "reasoningText": "...", "toolRequests": [...]},
         "id": ..., "timestamp": ..., "parentId": ...}

    We harvest the human-readable text fields (content, reasoningText, plus any
    string inside toolRequests[].input) so the project-path heuristic has enough
    signal. Bounded read so we don't choke on the 4MB file.
    """
    parts: list[str] = []
    size = path.stat().st_size
    with path.open("r", encoding="utf-8", errors="replace") as f:
        if size > max_bytes:
            try:
                f.seek(size - max_bytes)
                f.readline()  # discard partial line
            except OSError:
                pass
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = obj.get("data") or {}
            if not isinstance(data, dict):
                continue
            # Top-level text fields.
            for key in ("content", "reasoningText", "text", "message"):
                v = data.get(key)
                if isinstance(v, str):
                    parts.append(v)
            # Tool requests: their input often contains file paths.
            tool_requests = data.get("toolRequests") or []
            if isinstance(tool_requests, list):
                for tr in tool_requests:
                    if not isinstance(tr, dict):
                        continue
                    inp = tr.get("input") or tr.get("arguments")
                    if isinstance(inp, dict):
                        for v in inp.values():
                            if isinstance(v, str):
                                parts.append(v)
                    elif isinstance(inp, str):
                        parts.append(inp)
    return "\n".join(parts)


def score_projects(text: str, projects: dict[str, Path]) -> dict[str, int]:
    """Count occurrences of each project root (and its basename) in text.

    Counts both the absolute path and the bare basename, but gives higher
    weight to absolute path hits (more reliable signal).
    """
    scores: dict[str, int] = defaultdict(int)
    lowered = text.lower()
    for pid, root in projects.items():
        root_str = str(root)
        name = root.name.lower()
        # Absolute path occurrences (weight 3).
        abs_hits = text.count(root_str)
        # Basename occurrences as a standalone token (weight 1) — only when
        # preceded by a separator to avoid substring false positives.
        name_hits = len(re.findall(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", lowered))
        # Strip leading 'auto-' fallback project_ids from matching (they look
        # like hashes and never appear in transcripts).
        if pid.startswith("auto-"):
            continue
        total = abs_hits * 3 + name_hits
        if total > 0:
            scores[pid] = total
    return dict(scores)


def assign_project(scores: dict[str, int]) -> str | None:
    """Pick the winning project_id from scores. None if no signal."""
    if not scores:
        return None
    # Highest score wins; ties broken by alphabetical order (deterministic).
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def harvest(
    transcripts_glob: str,
    projects: dict[str, Path],
    *,
    min_bytes: int = 0,
    limit: int | None = None,
) -> list[dict]:
    """Discover, score, and return the validation plan."""
    # Expand and deduplicate transcripts.
    paths = sorted(
        Path(p).expanduser().resolve()
        for p in _glob(transcripts_glob)
        if Path(p).is_file()
    )
    if min_bytes > 0:
        paths = [p for p in paths if p.stat().st_size >= min_bytes]
    # Most recent first (validation campaign cares about recent sessions).
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit:
        paths = paths[:limit]

    plan: list[dict] = []
    for path in paths:
        text = extract_text_from_transcript(path)
        scores = score_projects(text, projects)
        winner = assign_project(scores)
        entry = {
            "session_id": path.stem,
            "transcript_path": str(path),
            "project_id": winner,
            "project_root": str(projects[winner]) if winner else None,
            "score": dict(sorted(scores.items(), key=lambda kv: -kv[1])),
            "bytes": path.stat().st_size,
            "mtime_iso": _iso(path.stat().st_mtime),
        }
        plan.append(entry)
    return plan


def _glob(pattern: str) -> list[str]:
    """Glob that supports ~ and returns paths."""
    from glob import glob
    return glob(Path(pattern).expanduser().as_posix())


def _iso(epoch: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=str(DEFAULT_BASE),
                    help="base dir containing workspace folders (default: %(default)s)")
    ap.add_argument("--glob", default=DEFAULT_TRANSCRIPTS_GLOB,
                    help="glob for transcripts (default: %(default)s)")
    ap.add_argument("--min-bytes", type=int, default=50_000,
                    help="skip tiny transcripts (default: 50KB)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the plan to N most recent sessions")
    ap.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    args = ap.parse_args()

    projects = discover_projects(Path(args.base))
    if not projects:
        print("[error] no projects found under", args.base, file=sys.stderr)
        return 2

    print(f"[info] discovered {len(projects)} projects: {sorted(projects)}", file=sys.stderr)

    plan = harvest(
        args.glob, projects,
        min_bytes=args.min_bytes, limit=args.limit,
    )

    # Summary on stderr.
    counts: Counter = Counter(e["project_id"] or "unknown" for e in plan)
    print(f"[info] {len(plan)} sessions planned. Distribution:", file=sys.stderr)
    for pid, n in counts.most_common():
        print(f"   {pid:20} {n}", file=sys.stderr)

    indent = 2 if args.pretty else None
    print(json.dumps(plan, indent=indent, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
