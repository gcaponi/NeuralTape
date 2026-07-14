"""Test per lex/v3/storage.py (D0.3)."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from storage import Episode, Storage  # type: ignore[import-not-found]


def _fresh_db() -> Storage:
    d = Path(tempfile.mkdtemp(prefix="nt-v3-stor-"))
    return Storage(d / "test.db")


def test_roundtrip_episode():
    s = _fresh_db()
    ep = Episode(
        project_id="zeus", kind="episodic", source_type="transcript",
        title="Test insight", body="body text", category="decision",
        confidence=0.8, raw_payload={"foo": "bar"},
    )
    eid = s.put_episode(ep)
    got = s.get_episode(eid)
    assert got is not None
    assert got.id == eid
    assert got.project_id == "zeus"
    assert got.kind == "episodic"
    assert got.title == "Test insight"
    assert got.body == "body text"
    assert got.confidence == 0.8
    assert got.raw_payload == {"foo": "bar"}


def test_invalid_kind_rejected():
    s = _fresh_db()
    ep = Episode(project_id="zeus", kind="bogus", source_type="manual", title="x")
    try:
        s.put_episode(ep)
    except ValueError:
        return
    raise AssertionError("expected ValueError for invalid kind")


def test_project_isolation():
    """Episodes from project A must not appear in project B queries."""
    s = _fresh_db()
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="transcript", title="Z1"))
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="transcript", title="Z2"))
    s.put_episode(Episode(project_id="cais-lp", kind="working", source_type="transcript", title="C1"))

    z = s.query_episodes("zeus")
    c = s.query_episodes("cais-lp")
    assert len(z) == 2
    assert len(c) == 1
    assert all(e.project_id == "zeus" for e in z)
    assert all(e.project_id == "cais-lp" for e in c)


def test_query_by_kind_filter():
    s = _fresh_db()
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="w"))
    s.put_episode(Episode(project_id="zeus", kind="episodic", source_type="t", title="e"))
    s.put_episode(Episode(project_id="zeus", kind="semantic", source_type="t", title="s"))

    working = s.query_episodes("zeus", kind="working")
    episodic = s.query_episodes("zeus", kind="episodic")
    assert len(working) == 1
    assert len(episodic) == 1
    assert working[0].title == "w"


def test_promote_episode():
    s = _fresh_db()
    eid = s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="x"))
    ok = s.promote_episode(eid, "episodic")
    assert ok is True
    got = s.get_episode(eid)
    assert got is not None
    assert got.kind == "episodic"
    assert got.updated_at >= got.created_at


def test_promote_unknown_returns_false():
    s = _fresh_db()
    ok = s.promote_episode("nonexistent-id", "episodic")
    assert ok is False


def test_query_since_filter():
    s = _fresh_db()
    t0 = time.time()
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="old",
                          created_at=t0 - 1000))
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="new",
                          created_at=t0))
    recent = s.query_episodes("zeus", since=t0 - 10)
    assert len(recent) == 1
    assert recent[0].title == "new"


def test_stats_by_kind():
    s = _fresh_db()
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="w1"))
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="w2"))
    s.put_episode(Episode(project_id="zeus", kind="episodic", source_type="t", title="e1"))
    stats = s.stats("zeus")
    assert stats.get("working") == 2
    assert stats.get("episodic") == 1
    assert stats.get("semantic", 0) == 0


def test_schema_version_recorded():
    s = _fresh_db()
    import sqlite3
    with sqlite3.connect(s.db_path) as c:
        row = c.execute("SELECT version FROM schema_version").fetchone()
    assert row is not None
    assert row[0] == 1
