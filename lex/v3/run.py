#!/usr/bin/env python3
"""NeuralTape v3 — entry point / self-check (Fase 0).

In Fase 0 v3 has no cron loop yet (that's Fase 1). This module provides:
    --selfcheck       smoke test: load config, init storage, verify coexistence.
    --bootstrap ...   delegates to bootstrap_projects.py.
    --status          print current v3 status (config + storage + cost).

It does NOT touch v2.2 cron. v2.2 keeps running on its own timer regardless.

Usage:
    python lex/v3/run.py --selfcheck
    NEURAL_TAPE_V3=1 python lex/v3/run.py --status
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent          # lex/v3/
TAPE_ROOT = THIS_DIR.parent.parent                  # NeuralTape/

log = logging.getLogger("neural-tape-v3")


def _load_sibling(name: str):
    """Load a sibling module of this file (lex/v3/<name>.py)."""
    return _load_from_path(f"nt_v3.{name}", THIS_DIR / f"{name}.py")


def _load_from_path(mod_name: str, path: Path):
    """Load a Python module from an arbitrary file path using importlib."""
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def selfcheck() -> int:
    """Smoke test all v3 components (Fase 0 + Fase 1)."""
    print("NeuralTape v3 - self-check (Fase 0 + Fase 1)")
    print("=" * 55)

    # -- Fase 0 --
    config_mod = _load_sibling("config")
    storage_mod = _load_sibling("storage")
    project_mod = _load_sibling("project")
    redaction_mod = _load_sibling("redaction")
    events_mod = _load_sibling("events")
    cost_mod = _load_sibling("cost")

    # 1. Config load
    cfg = config_mod.load(TAPE_ROOT)
    print(f"[1/10] config loaded - enabled={cfg.enabled}")
    print(f"       storage.db_path = {cfg.storage.db_path}")
    print(f"       cost limits     = {cfg.cost.daily_limit_calls} calls / {cfg.cost.daily_limit_tokens} tokens")

    # 2. Storage init
    storage = storage_mod.Storage(cfg.storage.db_path)
    stats = storage.stats()
    print(f"[2/10] storage OK - db={cfg.storage.db_path.name}, episodes by kind: {stats or 'empty'}")

    # 3. Project resolver
    resolver = project_mod.ProjectResolver()
    self_proj = resolver.resolve(TAPE_ROOT)
    print(f"[3/10] project resolver OK - self={self_proj.project_id} ({self_proj.source})")

    # 4. Redactor
    redactor = redaction_mod.Redactor(extra_patterns=cfg.redaction.extra_patterns)
    sample = "token=AKIAIOSFODNN7EXAMPLE and password=thisisalongsecret12345"
    redacted, events = redactor.redact(sample)
    print(f"[4/10] redactor OK - {len(events)} redaction(s) in sample")

    # 5. EventBus
    bus = events_mod.EventBus(storage, allowed_sources=set(cfg.events.enabled_sources))
    print(f"[5/10] event bus OK - active sources: {sorted(bus.allowed)}")

    # 6. Cost policy
    state_dir = cfg.storage.db_path.parent / ".state"
    policy = cost_mod.CostPolicy(
        budget=cost_mod.CostBudget(
            daily_limit_calls=cfg.cost.daily_limit_calls,
            daily_limit_tokens=cfg.cost.daily_limit_tokens,
        ),
        state_dir=state_dir,
        fallback_notify_interval_hours=cfg.cost.fallback_notify_interval_hours,
    )
    status = policy.status()
    print(f"[6/10] cost policy OK - {status['calls_today']}/{status['calls_limit']} calls today")

    # -- Fase 1 --
    classifier_mod = _load_sibling("classifier")
    memory_mod = _load_sibling("memory")
    focus_mod = _load_sibling("focus")
    workset_mod = _load_sibling("workset")
    # Load git adapter from subdirectory
    git_mod = _load_from_path("nt_v3.git_adapter", THIS_DIR / "adapters" / "git.py")
    GitAdapter = git_mod.GitAdapter

    # 7. ClassifierV3 - instantiation test (no actual LLM call)
    clf = classifier_mod.ClassifierV3(
        config=cfg, project=self_proj, storage=storage,
        redactor=redactor, cost_policy=policy,
        base_url="http://localhost:9999/noop",
        api_key="test-key-0000",
        model="test-model",
    )
    print(f"[7/10] classifier instantiation OK - model={clf.model}")

    # 8. MemoryPromoter - tick (sweeps existing working episodes)
    promoter = memory_mod.MemoryPromoter(storage=storage, config=cfg)
    stats_p = promoter.tick(project_id="neuraltape")
    print(f"[8/10] memory promoter OK - examined={stats_p.examined}")

    # 9. GitAdapter - verify NeuralTape root is a git repo
    git_adapter = GitAdapter(
        project_root=self_proj.root,
        event_bus=bus,
        project_id=self_proj.project_id,
    )
    branch = git_adapter.get_current_branch()
    files = git_adapter.get_recent_files(max_files=5)
    print(f"[9/10] git adapter OK - branch={branch}, recent_files={len(files)}")

    # 10. FocusGenerator + WorkingSetGenerator - instantiation
    focus_dir = cfg.storage.db_path.parent / "focus"
    focus_gen = focus_mod.FocusGenerator(
        storage=storage, git_adapter=git_adapter,
        project=self_proj, output_dir=focus_dir,
    )
    ws_gen = workset_mod.WorkingSetGenerator(
        storage=storage, project_root=self_proj.root,
        project_id=self_proj.project_id, output_dir=focus_dir,
    )
    print(f"[10/10] focus + workset generators instantiated OK")

    # Coexistence check
    v22_state = TAPE_ROOT / "tape" / ".state" / "v22-session-state.json"
    print()
    print(f"v2.2 state file: {v22_state}")
    print(f"  exists: {v22_state.exists()} (v3 does NOT touch this)")
    print(f"v3 feature flag: NEURALTAPE_V3 env = {os.environ.get('NEURALTAPE_V3', '<unset>')}")
    print(f"  -> v3 currently {'ACTIVE' if cfg.enabled else 'DORMANT'}")

    print()
    print("OK self-check passed (Fase 0 + Fase 1).")
    return 0


def status() -> int:
    config_mod = _load_sibling("config")
    cfg = config_mod.load(TAPE_ROOT)
    print(f"v3 enabled          : {cfg.enabled}")
    print(f"v3 db               : {cfg.storage.db_path}")
    print(f"cost daily limits   : {cfg.cost.daily_limit_calls} calls / {cfg.cost.daily_limit_tokens} tokens")
    print(f"events sources      : {cfg.events.enabled_sources}")
    print(f"redaction extras    : {len(cfg.redaction.extra_patterns)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NeuralTape v3 entry point (Fase 0 + Fase 1)")
    ap.add_argument("--selfcheck", action="store_true", help="smoke test all v3 modules")
    ap.add_argument("--status", action="store_true", help="print current v3 config status")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.selfcheck:
        return selfcheck()
    if args.status:
        return status()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
