"""Discover recent transcripts produced by Copilot, Codex, Kimi Code and Grok Build."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import unquote


class TranscriptWatcher:
    """Find assistant transcripts across every supported local store."""

    # Kimi Code workspace dirs: wd_<workspace-folder-name>_<12-hex-hash>
    _KIMI_WD_RE = re.compile(r"^wd_(?P<label>.+)_[0-9a-f]{12}$")

    def __init__(
        self,
        *,
        home: Path | None = None,
        vscode_user: Path | None = None,
        codex_home: Path | None = None,
        kimi_home: Path | None = None,
        grok_home: Path | None = None,
    ):
        self.home = (home or Path.home()).expanduser()
        self.vscode_user = vscode_user or self.home / ".config" / "Code" / "User"
        self.codex_home = codex_home or self.home / ".codex"
        self.kimi_home = kimi_home or self.home / ".kimi-code"
        self.grok_home = grok_home or self.home / ".grok"

    def _paths(self):
        workspace_storage = self.vscode_user / "workspaceStorage"
        yield from workspace_storage.glob("*/GitHub.copilot-chat/transcripts/*.jsonl")
        yield from (self.codex_home / "sessions").glob("**/*.jsonl")
        yield from (self.codex_home / "archived_sessions").glob("*.jsonl")
        # Kimi Code: only the main agent wire. Subagent wires (agents/agent-N)
        # duplicate the same session and would share its session id.
        yield from (self.kimi_home / "sessions").glob("*/*/agents/main/wire.jsonl")
        # Grok Build: ~/.grok/sessions/<urlencoded-cwd>/<uuid>/chat_history.jsonl
        yield from (self.grok_home / "sessions").glob("*/*/chat_history.jsonl")

    def find_active_transcript(self, max_age_minutes: int = 60) -> Path | None:
        candidates = self.find_all_transcripts(max_age_minutes=max_age_minutes)
        return candidates[0][1] if candidates else None

    def find_all_transcripts(self, max_age_minutes: int = 60) -> list[tuple[float, Path]]:
        now = time.time()
        candidates: dict[Path, float] = {}
        for path in self._paths():
            try:
                resolved = path.resolve()
                mtime = resolved.stat().st_mtime
            except OSError:
                continue
            if (now - mtime) / 60 >= max_age_minutes:
                continue
            if not self._should_index(resolved):
                continue
            candidates[resolved] = mtime
        return sorted(((mtime, path) for path, mtime in candidates.items()), reverse=True)

    def get_workspace_label(self, transcript: Path) -> str:
        transcript = Path(transcript).resolve()
        if self.codex_home.resolve() in transcript.parents:
            cwd = self._codex_cwd(transcript)
            return Path(cwd).name if cwd else "unknown"

        grok_sessions = (self.grok_home / "sessions").resolve()
        if grok_sessions in transcript.parents:
            return self._grok_workspace_label(transcript)

        kimi_sessions = (self.kimi_home / "sessions").resolve()
        if kimi_sessions in transcript.parents:
            for parent in transcript.parents:
                if parent.parent == kimi_sessions:
                    return self._kimi_workspace_label(parent.name)
            return "unknown"

        hash_dir = transcript.parent.parent.parent
        workspace_json = hash_dir / "workspace.json"
        if workspace_json.exists():
            try:
                data = json.loads(workspace_json.read_text(encoding="utf-8"))
                folder = data.get("folder") or data.get("workspace")
                if folder:
                    return Path(folder).name
            except (json.JSONDecodeError, OSError):
                pass
        return hash_dir.name[:8]

    @classmethod
    def _kimi_workspace_label(cls, dirname: str) -> str:
        match = cls._KIMI_WD_RE.match(dirname)
        return match.group("label") if match else dirname

    @staticmethod
    def _codex_cwd(transcript: Path) -> str | None:
        try:
            with Path(transcript).open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "session_meta":
                        continue
                    payload = event.get("payload") or {}
                    cwd = payload.get("cwd") if isinstance(payload, dict) else None
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            return None
        return None

    def _should_index(self, transcript: Path) -> bool:
        """Drop subagent transcripts that would duplicate a parent session."""
        try:
            transcript.relative_to(self.grok_home.resolve())
        except ValueError:
            pass
        else:
            return not self._grok_is_subagent(transcript.parent)

        try:
            transcript.relative_to(self.codex_home.resolve())
        except ValueError:
            return True
        return not self._codex_is_subagent(transcript)

    @staticmethod
    def _grok_is_subagent(session_dir: Path) -> bool:
        summary = session_dir / "summary.json"
        if not summary.exists():
            return False
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if data.get("parent_session_id"):
            return True
        # Harness workers (plan writer, verifier) have no parent id but
        # are not a Lex conversation.
        text = str(data.get("session_summary") or "")
        return "Harness" in text or "Goal Plan Writer" in text or "verifier" in text.lower()

    @staticmethod
    def _codex_is_subagent(transcript: Path) -> bool:
        try:
            with Path(transcript).open("r", encoding="utf-8", errors="replace") as stream:
                for _ in range(20):
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "session_meta":
                        continue
                    payload = event.get("payload") or {}
                    if not isinstance(payload, dict):
                        return False
                    if payload.get("forked_from_id") or payload.get("agent_nickname"):
                        return True
                    source = payload.get("source")
                    return isinstance(source, dict) and "subagent" in source
        except OSError:
            return False
        return False

    @staticmethod
    def _grok_workspace_label(transcript: Path) -> str:
        # ~/.grok/sessions/<urlencoded-cwd>/<uuid>/chat_history.jsonl
        encoded = transcript.parent.parent.name
        decoded = unquote(encoded)
        name = Path(decoded).name
        return name or "unknown"

    @staticmethod
    def get_session_id(transcript: Path) -> str:
        path = Path(transcript)
        if path.name == "wire.jsonl":
            # Kimi Code layout: sessions/wd_*/session_<uuid>/agents/<agent>/wire.jsonl
            # The file stem ("wire") is identical across sessions; the unique id
            # lives in the session_<uuid> directory name.
            for parent in path.parents:
                if parent.name.startswith("session_"):
                    return parent.name
        if path.name == "chat_history.jsonl":
            return path.parent.name
        return path.stem
