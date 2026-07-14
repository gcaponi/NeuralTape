"""Test per lex/v3/adapters/git.py (D1.3).

Uses a temporary git repository to test git operations without polluting
the real workspace.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "lex" / "v3"))

# Suppress logging noise during tests
logging.disable(logging.CRITICAL)

from adapters.git import GitAdapter, GitCommitEvent  # type: ignore[import-not-found]


def _init_git_repo(path: Path) -> Path:
    """Initialize a temporary git repo at path with an initial commit. Returns path."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@nt.local"], cwd=path,
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path,
                   capture_output=True, check=True)
    # Make an initial commit so the branch name is set
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitkeep"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=path,
                   capture_output=True, check=True)
    return path


def _make_commit(repo: Path, file_name: str, content: str, msg: str) -> str:
    """Create a file and commit it. Returns SHA."""
    (repo / file_name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", file_name], cwd=repo, capture_output=True, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", msg], cwd=repo, capture_output=True, text=True, check=True,
    )
    # Extract short SHA from output
    for line in result.stdout.splitlines():
        if line.strip().startswith("["):
            parts = line.strip().split()
            if len(parts) >= 2:
                return parts[1].rstrip("]")
    return ""


def _make_adapter(repo: Path) -> GitAdapter:
    """Create a minimal GitAdapter for a test repo."""
    # EventBus stub (to avoid needing storage for tests)
    class FakeEventBus:
        def publish(self, event):
            return 0
        allowed = {"git.commit"}
    return GitAdapter(
        project_root=repo,
        event_bus=FakeEventBus(),
        project_id="test-proj",
    )


def test_get_current_branch():
    repo = Path(tempfile.mkdtemp(prefix="nt-v3-git-"))
    _init_git_repo(repo)
    adapter = _make_adapter(repo)
    branch = adapter.get_current_branch()
    assert branch in ("main", "master"), f"unexpected branch: {branch!r}"


def test_poll_commits_since_future():
    """Poll with a 'since' time in the future returns nothing (no new commits)."""
    repo = Path(tempfile.mkdtemp(prefix="nt-v3-git-"))
    _init_git_repo(repo)
    adapter = _make_adapter(repo)
    # Poll from 1 hour in the future
    events = adapter.poll_commits(since_epoch=time.time() + 3600)
    assert events == []


def test_poll_commits_single_commit():
    repo = Path(tempfile.mkdtemp(prefix="nt-v3-git-"))
    _init_git_repo(repo)
    _make_commit(repo, "hello.py", "print('hi')", "feat: add hello.py")
    adapter = _make_adapter(repo)
    events = adapter.poll_commits(since_epoch=time.time() - 3600)
    assert len(events) >= 1
    assert "feat:" in events[0].message_short
    assert "hello.py" in events[0].files_changed or not events[0].files_changed


def test_poll_commits_multiple():
    repo = Path(tempfile.mkdtemp(prefix="nt-v3-git-"))
    _init_git_repo(repo)
    _make_commit(repo, "a.py", "a", "first commit")
    _make_commit(repo, "b.py", "b", "second commit")
    _make_commit(repo, "c.py", "c", "third commit")
    adapter = _make_adapter(repo)
    events = adapter.poll_commits(since_epoch=time.time() - 3600)
    assert len(events) >= 3


def test_get_recent_files_with_uncommitted():
    repo = Path(tempfile.mkdtemp(prefix="nt-v3-git-"))
    _init_git_repo(repo)
    _make_commit(repo, "existing.py", "old", "initial")
    # Uncommitted change
    (repo / "new_file.py").write_text("new", encoding="utf-8")
    adapter = _make_adapter(repo)
    files = adapter.get_recent_files(max_files=10)
    assert "new_file.py" in files
    assert "existing.py" in files


def test_parse_log():
    """Test the _parse_log method with a simulated git log output.
    Uses a valid git repo (needed because GitAdapter.__init__ checks git root).
    """
    repo = Path(tempfile.mkdtemp(prefix="nt-v3-git-"))
    _init_git_repo(repo)
    # Make an initial commit so the repo has history
    _make_commit(repo, "init.py", "x", "init")
    adapter = _make_adapter(repo)
    fake_log = (
        "abc123||Test Author||test@x.com||2026-07-14T10:00:00||feat: add feature\n"
        "\n"
        "src/main.py\n"
        "src/utils.py\n"
        "\n"
        "def456||Other||other@x.com||2026-07-14T09:00:00||fix: resolve bug\n"
        "\n"
        "src/bug.py\n"
    )
    events = adapter._parse_log(fake_log)
    assert len(events) == 2
    assert events[0].sha == "abc123"
    assert events[0].author == "Test Author"
    assert "src/main.py" in events[0].files_changed
    assert events[1].sha == "def456"
    assert events[1].message_short == "fix: resolve bug"
