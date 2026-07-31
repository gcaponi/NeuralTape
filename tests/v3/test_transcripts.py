"""Regression tests for Copilot + Codex transcript ingestion."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from nt_v3.transcript_parser import TranscriptParser
from nt_v3.transcript_watcher import TranscriptWatcher


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


def test_parser_keeps_legacy_copilot_compatibility():
    with tempfile.TemporaryDirectory(prefix="nt-copilot-parser-") as tmp:
        transcript = Path(tmp) / "legacy.jsonl"
        _write_jsonl(
            transcript,
            [
                {
                    "type": "user.message",
                    "timestamp": "2026-07-30T10:00:00Z",
                    "data": {"content": "Apri EterCervo"},
                },
                {
                    "type": "assistant.message",
                    "timestamp": "2026-07-30T10:01:00Z",
                    "data": {"reasoningText": "Verifico prima", "content": "Fatto"},
                },
            ],
        )

        parsed = TranscriptParser().parse_delta(transcript)

        assert "[USER]\nApri EterCervo" in parsed
        assert "[LEX reasoning]\nVerifico prima" in parsed
        assert "[LEX]\nFatto" in parsed


def test_parser_reads_codex_rollout_without_system_or_tool_outputs():
    with tempfile.TemporaryDirectory(prefix="nt-codex-parser-") as tmp:
        transcript = Path(tmp) / "rollout-test.jsonl"
        _write_jsonl(
            transcript,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-31T10:00:00Z",
                    "payload": {"cwd": "/work/EterCervo", "source": "vscode"},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-31T10:01:00Z",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "ISTRUZIONI INTERNE"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-31T10:02:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Sistema Neural Tape"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-31T10:03:00Z",
                    "payload": {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Cerco la causa"}],
                        "encrypted_content": "opaque",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-31T10:04:00Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Problema corretto"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-31T10:05:00Z",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": "{\"cmd\":\"git status\"}",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-31T10:06:00Z",
                    "payload": {
                        "type": "function_call_output",
                        "output": "SEGRETO-DA-NON-INGERIRE",
                    },
                },
            ],
        )

        parser = TranscriptParser()
        parsed = parser.parse_delta(transcript)
        counts = parser.parse_delta_structured(transcript)

        assert "cwd: /work/EterCervo" in parsed
        assert "[USER]\nSistema Neural Tape" in parsed
        assert "[LEX reasoning]\nCerco la causa" in parsed
        assert "[LEX]\nProblema corretto" in parsed
        assert "[TOOL → exec_command]" in parsed
        assert "ISTRUZIONI INTERNE" not in parsed
        assert "SEGRETO-DA-NON-INGERIRE" not in parsed
        assert counts == {
            "user": 1,
            "assistant": 1,
            "reasoning": 1,
            "tool_calls": 1,
            "total_events": 7,
        }


def test_watcher_discovers_copilot_and_codex_transcripts():
    with tempfile.TemporaryDirectory(prefix="nt-watcher-") as tmp:
        home = Path(tmp)
        codex = home / ".codex" / "sessions" / "2026" / "07" / "31" / "rollout.jsonl"
        copilot = (
            home
            / ".config"
            / "Code"
            / "User"
            / "workspaceStorage"
            / "abc"
            / "GitHub.copilot-chat"
            / "transcripts"
            / "legacy.jsonl"
        )
        _write_jsonl(
            codex,
            [{"type": "session_meta", "payload": {"cwd": "/work/EterCervo"}}],
        )
        _write_jsonl(copilot, [{"type": "user.message", "data": {"content": "ciao"}}])

        watcher = TranscriptWatcher(home=home)
        found = watcher.find_all_transcripts(max_age_minutes=60)
        paths = {path for _, path in found}

        assert codex.resolve() in paths
        assert copilot.resolve() in paths
        assert watcher.get_workspace_label(codex) == "EterCervo"


def test_harvester_assigns_codex_project_from_session_cwd():
    from tools.harvest_sessions import harvest

    with tempfile.TemporaryDirectory(prefix="nt-harvest-codex-") as tmp:
        root = Path(tmp)
        project = root / "EterCervo"
        project.mkdir()
        transcript = root / "rollout-project.jsonl"
        _write_jsonl(
            transcript,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-31T10:00:00Z",
                    "payload": {"cwd": str(project), "source": "vscode"},
                }
            ],
        )

        plan = harvest(
            str(transcript),
            {"etercervo": project},
            min_bytes=0,
            limit=None,
        )

        assert len(plan) == 1
        assert plan[0]["project_id"] == "etercervo"
        assert plan[0]["project_root"] == str(project)
