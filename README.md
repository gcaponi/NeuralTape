# Neural Tape v2.2

Automatic memory layer for VS Code Copilot sessions.

Neural Tape reads the JSONL transcripts that VS Code already writes to disk, waits until the session becomes idle, classifies the useful long-term insights with an OpenAI-compatible LLM, then writes them to Lex memory and the local archive.

## What changed in v2.2

Previous versions watched assistant-specific CLI logs from Kimi Code, OpenCode, and ZCode. Guglielmo now works primarily in VS Code Chat, so v2.2 uses the real source of truth:

```text
~/.config/Code/User/workspaceStorage/<hash>/GitHub.copilot-chat/transcripts/<session-id>.jsonl
```

The flow is fully automatic:

```text
VS Code transcript -> 5-minute user timer -> idle detection -> DeepSeek classifier -> memory.md + tape/archive
```

## Live components

```text
lex/pre_load.py             Startup context generator
lex/v22/watcher.py          Finds active VS Code transcripts
lex/v22/session_detector.py Tracks offsets and idle state
lex/v22/transcript_parser.py Converts JSONL to LLM-readable text
lex/v22/classifier.py       Calls the LLM using stdlib urllib
lex/v22/memory_writer.py    Writes memory.md and archive entries
lex/v22/notifier.py         Emits local notifications/log messages
lex/v22/run.py              One-shot orchestrator
lex/v22/run-cron.sh         systemd/cron-safe wrapper with flock
```

## Quick start

Create local `.env` and `config.yaml` files. Do not commit them.

```bash
cd /path/to/NeuralTape
cp .env.example .env
cp config.example.yaml config.yaml
```

Set `paths.neural_tape_root`, `paths.etervelo_wiki`, and `paths.lex_memory` in `config.yaml`.

Preview a session without writing memory:

```bash
ETERCERVO_ROOT=/path/to/EterCervo /usr/bin/python lex/v22/run.py --once <session-id-prefix> --preview -v
```

On Guglielmo's machine the timer is installed as:

```bash
systemctl --user status neural-tape-v22.timer
tail -40 tape/.state/v22-cron.log
```

## Runtime data

Runtime files are intentionally ignored by Git:

- `.env`
- `config.yaml`
- `session-context.md`
- `tape/.state/`
- `tape/sessions/`
- `tape/staging/`
- `tape/archive/`
- `logs/`

Only `.gitkeep` placeholders are tracked for the tape directories.

## Legacy

The v1.2 log-parser pipeline is archived under `legacy/v1.2/`. It is kept for reference and possible rollback, but the active workflow is v2.2.

## License

MIT - see `LICENSE`.
