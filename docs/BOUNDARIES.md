# Neural Tape <-> Lex Memory Boundary Contract

> Version: 2.2
> Date: 2026-07-08
> Status: Active

## The Contract

| Dimension | Neural Tape v2.2 | `_Lex/memory.md` |
|---|---|---|
| Source | VS Code Copilot transcript JSONL | Curated operational memory |
| Author | Automated classifier acting as Lex | Lex + automated Neural Tape writer |
| Input | `user.message`, `assistant.message`, `reasoningText`, tool events | High-value patterns, decisions, warnings, preferences |
| Curation | LLM classifier, strict prompt, max 5-7 insights/session | `Recent Context` + category sections |
| Storage | `tape/archive/<category>/` | `_Lex/memory.md` |
| Trigger | User systemd timer every 5 minutes, classify only when idle | Startup read + automatic writes |

## Rules

### 1. Guglielmo does not classify manually

Memory selection is Lex work. Neural Tape reads the session transcript and decides what is worth keeping.

### 2. Neural Tape may write `_Lex/memory.md`

This is the v2.2 change. The writer inserts each accepted insight into `Recent Context` and the matching category section, then creates a full archive file.

### 3. Runtime data stays local

Do not commit `.env`, `session-context.md`, `tape/.state/`, transcript-derived session files, or archive content. Only placeholders are tracked.

### 4. `pre_load.py` remains the startup bridge

Lex still runs `neural-tape/lex/pre_load.py` at startup and reads `session-context.md` to rebuild short-term context.

### 5. v1.2 is legacy

`log_parser.py`, `post_capture.py`, and `deja_vu.py` live under `legacy/v1.2/`. They are not part of the default workflow.

## Operational Flow

```text
VS Code Chat session
    -> transcript JSONL on disk
    -> neural-tape-v22.timer every 5 minutes
    -> SessionDetector waits until idle
    -> TranscriptParser formats the delta
    -> LLMClassifier extracts high-value insights
    -> MemoryWriter updates memory.md + tape/archive
    -> pre_load.py exposes context at next startup
```

## Changelog

- 2026-07-08 v2.2: Switched from CLI-log staging/review to automatic VS Code transcript classification.
- 2026-06-07 v1.0: Initial Neural Tape / Lex Memory boundary.
