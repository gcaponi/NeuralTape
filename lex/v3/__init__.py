"""NeuralTape v3 — Fase 0 Foundation layer.

Modules:
- config    : v3 config loader (extends v2.2 config.yaml)
- project   : project identity resolver (Q4=C, config-first + hash fallback)
- redaction : secret redaction before LLM payload (D0.1)
- storage   : SQLite persistent layer for episodes (D0.3)
- events    : EventBus minimale, 2 source types Fase 0 (D0.4)
- cost      : cost/fallback policy for LLM calls (D0.5)

Coexists with lex.v22. Activated via feature flag `v3.enabled` in config.yaml
or env NEURALTAPE_V3=1. Does NOT touch v2.2 cron when disabled.
"""

from __future__ import annotations

__version__ = "3.0.0-phase0"
