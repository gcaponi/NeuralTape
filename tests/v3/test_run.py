"""Test per l'orchestratore one-shot di NeuralTape v3."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from nt_v3.run import _latest_transcript_window, resolve_transcript, run_once
from nt_v3.storage import Episode, Storage


class FakeClassifier:
    calls = 0

    def __init__(self, *, storage: Storage, **_kwargs):
        self.storage = storage

    def classify_and_persist(
        self,
        transcript_text: str,
        session_id: str,
        project_id: str,
    ) -> int:
        type(self).calls += 1
        self.storage.put_episode(
            Episode(
                project_id=project_id,
                kind="episodic",
                source_type="transcript",
                source_ref=session_id,
                category="decision",
                title="Attivare orchestratore Neural Tape v3",
                body="La v3 deve processare sessioni reali in modo idempotente.",
                confidence=0.9,
            )
        )
        return 1


def _init_git_repo(project_root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=project_root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "neural-tape-test@example.invalid"],
        cwd=project_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Neural Tape Test"],
        cwd=project_root,
        check=True,
    )
    (project_root / "README.md").write_text("# Test project\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=project_root, check=True)


def test_run_once_persists_context_and_is_idempotent():
    FakeClassifier.calls = 0
    with tempfile.TemporaryDirectory(prefix="nt-v3-run-") as tmp:
        temp_root = Path(tmp)
        tape_root = temp_root / "neural-tape"
        project_root = temp_root / "project"
        transcript = temp_root / "session-123.jsonl"
        tape_root.mkdir()
        project_root.mkdir()
        _init_git_repo(project_root)

        project_config = project_root / ".neuraltape" / "project.yaml"
        project_config.parent.mkdir()
        project_config.write_text(
            "project_id: test-project\ndisplay_name: Test Project\n",
            encoding="utf-8",
        )
        config_path = tape_root / "config.yaml"
        config_path.write_text(
            "v3:\n"
            "  enabled: true\n"
            "  storage:\n"
            "    db_path: tape/v3/neuraltape.db\n",
            encoding="utf-8",
        )
        transcript.write_text(
            json.dumps(
                {
                    "type": "user.message",
                    "timestamp": "2026-07-15T12:00:00Z",
                    "data": {"content": "Attiviamo Neural Tape v3"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        first = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        second = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )

        storage = Storage(tape_root / "tape" / "v3" / "neuraltape.db")
        episodes = storage.query_episodes("test-project")
        output_dir = tape_root / "tape" / "v3" / "projects" / "test-project"

        assert first.episodes_written == 1
        assert first.skipped is False
        assert second.episodes_written == 0
        assert second.skipped is True
        assert FakeClassifier.calls == 1
        assert len(episodes) == 1
        assert episodes[0].source_ref == "session-123"
        assert (output_dir / "current-focus.json").exists()
        assert (output_dir / "working-set.json").exists()


def test_run_once_reprocesses_when_transcript_grows():
    """Regression: a session classified too early (eps=0 on a short snapshot)
    must be reprocessed when the transcript grows beyond the threshold.

    Previously the `transcript.classified` marker was written unconditionally,
    freezing the session forever. The fix stores `transcript_bytes` and only
    skips when the size has not changed significantly.
    """
    FakeClassifier.calls = 0
    with tempfile.TemporaryDirectory(prefix="nt-v3-run-") as tmp:
        temp_root = Path(tmp)
        tape_root = temp_root / "neural-tape"
        project_root = temp_root / "project"
        transcript = temp_root / "session-grow.jsonl"
        tape_root.mkdir()
        project_root.mkdir()
        _init_git_repo(project_root)

        project_config = project_root / ".neuraltape" / "project.yaml"
        project_config.parent.mkdir()
        project_config.write_text(
            "project_id: test-grow\n display_name: Test Grow\n",
            encoding="utf-8",
        )
        config_path = tape_root / "config.yaml"
        config_path.write_text(
            "v3:\n  enabled: true\n  storage:\n    db_path: tape/v3/neuraltape.db\n",
            encoding="utf-8",
        )

        # Initial short snapshot (no real insights yet)
        transcript.write_text(
            json.dumps(
                {
                    "type": "user.message",
                    "timestamp": "2026-07-15T12:00:00Z",
                    "data": {"content": "Attiviamo Neural Tape v3"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        first = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert first.skipped is False
        assert FakeClassifier.calls == 1

        # No growth -> must stay skipped
        unchanged = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert unchanged.skipped is True
        assert FakeClassifier.calls == 1

        # Append a large block of new content (> GROWTH_THRESHOLD_BYTES = 2KB)
        with open(transcript, "a", encoding="utf-8") as fh:
            for i in range(200):
                fh.write(
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "timestamp": "2026-07-15T13:00:00Z",
                            "data": {"content": f"Nuova attività session {i:03d} " * 5},
                        }
                    )
                    + "\n"
                )

        grown = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert grown.skipped is False, "growing session must be reprocessed"
        assert FakeClassifier.calls == 2, "classifier must run again after growth"


def test_resolve_transcript_rejects_ambiguous_prefix():
    class FakeWatcher:
        def find_all_transcripts(self, max_age_minutes: int):
            assert max_age_minutes == 60
            return [
                (2.0, Path("/tmp/session-abc-one.jsonl")),
                (1.0, Path("/tmp/session-abc-two.jsonl")),
            ]

    try:
        resolve_transcript("session-abc", watcher=FakeWatcher(), max_age_minutes=60)
    except ValueError as error:
        assert "ambiguous" in str(error).lower()
    else:
        raise AssertionError("ambiguous session prefix was accepted")


def test_resolve_transcript_prefers_exact_session_id():
    exact = Path("/tmp/session-abc.jsonl")

    class FakeWatcher:
        def find_all_transcripts(self, max_age_minutes: int):
            assert max_age_minutes == 60
            return [
                (2.0, Path("/tmp/session-abc-extra.jsonl")),
                (1.0, exact),
            ]

    resolved = resolve_transcript(
        "session-abc",
        watcher=FakeWatcher(),
        max_age_minutes=60,
    )

    assert resolved == exact


def test_latest_transcript_window_keeps_recent_context():
    transcript = "OLD-CONTEXT\n" + ("x" * 100) + "\nNEW-CONTEXT"

    window = _latest_transcript_window(transcript, max_chars=40)

    assert len(window) == 40
    assert "OLD-CONTEXT" not in window
    assert window.endswith("NEW-CONTEXT")