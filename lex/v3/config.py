"""config — v3 config loader.

Extends v2.2 config.yaml with a `v3:` section. Falls back to safe defaults if
the section is missing, so v3 never crashes because of an old config file.

Feature flag resolution order (first wins):
    1. Env NEURALTAPE_V3=1|0 (explicit override)
    2. config.yaml: v3.enabled
    3. Default: False (v3 dormant, v2.2 untouched)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("neural-tape-v3")

DEFAULTS = {
    "enabled": False,
    "storage": {"db_path": "tape/v3/neuraltape.db"},
    "cost": {
        "daily_limit_calls": 100,
        "daily_limit_tokens": 200000,
        "fallback_notify_interval_hours": 24,
    },
    "events": {"enabled_sources": ["transcript", "git.commit"]},
    "redaction": {"extra_patterns": []},
    "memory": {
        "promote_threshold_working_to_episodic": 0.6,
        "promote_threshold_episodic_to_semantic": 0.8,
        "promote_min_age_hours": 4,
        "promote_min_similar_episodes": 2,
        "promote_min_sessions_for_semantic": 3,
        "working_ttl_hours": 48,
    },
}


@dataclass
class StorageConfig:
    db_path: Path

@dataclass
class CostConfig:
    daily_limit_calls: int
    daily_limit_tokens: int
    fallback_notify_interval_hours: int

@dataclass
class EventsConfig:
    enabled_sources: list[str]

@dataclass
class RedactionConfig:
    extra_patterns: list[tuple[str, str]] = field(default_factory=list)

@dataclass
class MemoryConfig:
    promote_threshold_working_to_episodic: float
    promote_threshold_episodic_to_semantic: float
    promote_min_age_hours: float
    promote_min_similar_episodes: int
    promote_min_sessions_for_semantic: int
    working_ttl_hours: float

@dataclass
class V3Config:
    enabled: bool
    tape_root: Path               # NeuralTape/ root, used to resolve relative paths
    storage: StorageConfig
    cost: CostConfig
    events: EventsConfig
    redaction: RedactionConfig
    memory: MemoryConfig


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins)."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_path(p: str | Path, tape_root: Path) -> Path:
    path = Path(p).expanduser()
    if path.is_absolute():
        return path
    return (tape_root / path).resolve()


def load(tape_root: Path, config_path: Path | None = None) -> V3Config:
    """Load v3 config. Never raises on missing/invalid section — uses defaults."""
    tape_root = tape_root.resolve()

    raw: dict = {}
    if config_path is None:
        config_path = tape_root / "config.yaml"
    if config_path.exists():
        try:
            full = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            raw = full.get("v3", {}) or {}
        except (yaml.YAMLError, OSError) as e:
            log.warning("config.yaml unreadable (%s); using v3 defaults", e)

    merged = _deep_merge(DEFAULTS, raw)

    # Feature flag resolution: env overrides config
    env_flag = os.environ.get("NEURALTAPE_V3")
    if env_flag is not None:
        enabled = env_flag.strip().lower() in ("1", "true", "yes", "on")
        log.info("NEURALTAPE_V3=%s → enabled=%s (env override)", env_flag, enabled)
    else:
        enabled = bool(merged["enabled"])

    # extra_patterns: list of [regex, kind] pairs → tuples
    extra = []
    for entry in merged["redaction"].get("extra_patterns", []):
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            extra.append((str(entry[0]), str(entry[1])))
        else:
            log.warning("Ignoring malformed redaction.extra_patterns entry: %r", entry)

    return V3Config(
        enabled=enabled,
        tape_root=tape_root,
        storage=StorageConfig(
            db_path=_resolve_path(merged["storage"]["db_path"], tape_root),
        ),
        cost=CostConfig(
            daily_limit_calls=int(merged["cost"]["daily_limit_calls"]),
            daily_limit_tokens=int(merged["cost"]["daily_limit_tokens"]),
            fallback_notify_interval_hours=int(merged["cost"]["fallback_notify_interval_hours"]),
        ),
        events=EventsConfig(
            enabled_sources=list(merged["events"]["enabled_sources"]),
        ),
        redaction=RedactionConfig(extra_patterns=extra),
        memory=MemoryConfig(
            promote_threshold_working_to_episodic=float(merged["memory"]["promote_threshold_working_to_episodic"]),
            promote_threshold_episodic_to_semantic=float(merged["memory"]["promote_threshold_episodic_to_semantic"]),
            promote_min_age_hours=float(merged["memory"]["promote_min_age_hours"]),
            promote_min_similar_episodes=int(merged["memory"]["promote_min_similar_episodes"]),
            promote_min_sessions_for_semantic=int(merged["memory"]["promote_min_sessions_for_semantic"]),
            working_ttl_hours=float(merged["memory"]["working_ttl_hours"]),
        ),
    )
