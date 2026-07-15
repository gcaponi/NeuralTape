"""storage — SQLite persistent layer for episodes (D0.3).

Episodes are the atomic unit of memory: a classified insight, a captured event
promoted to memory, a manual note. They have a ``kind`` (working/episodic/semantic)
that determines lifetime and retrieval priority.

Design choices:
- sqlite3 from stdlib (zero new dependencies, matches v2.2 philosophy).
- WAL journal mode for concurrent reader during cron writes.
- schema_version table for future migrations (Fase 0 = v1).
- Connection-per-operation via context manager: cron is short-lived, no pool needed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("neural-tape-v3")

SCHEMA_VERSION = 1

_SCHEMA_SQL = [
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)",

    """CREATE TABLE IF NOT EXISTS episodes (
        id              TEXT PRIMARY KEY,
        project_id      TEXT NOT NULL,
        kind            TEXT NOT NULL,
        source_type     TEXT NOT NULL,
        source_ref      TEXT,
        category        TEXT,
        title           TEXT NOT NULL,
        body            TEXT,
        confidence      REAL DEFAULT 0.0,
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL,
        raw_payload     TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ep_proj_kind ON episodes(project_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_ep_created ON episodes(created_at)",

    """CREATE TABLE IF NOT EXISTS focus_history (
        project_id      TEXT NOT NULL,
        captured_at     REAL NOT NULL,
        goal            TEXT,
        branch          TEXT,
        confidence      REAL,
        raw_payload     TEXT,
        PRIMARY KEY (project_id, captured_at)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_focus_proj ON focus_history(project_id, captured_at DESC)",

    """CREATE TABLE IF NOT EXISTS event_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      TEXT NOT NULL,
        source_type     TEXT NOT NULL,
        source_ref      TEXT,
        captured_at     REAL NOT NULL,
        payload         TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_evt_proj ON event_log(project_id, captured_at DESC)",
]

VALID_KINDS = {"working", "episodic", "semantic"}


@dataclass
class Episode:
    project_id: str
    kind: str               # 'working' | 'episodic' | 'semantic'
    source_type: str        # 'transcript' | 'git.commit' | 'manual' | ...
    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source_ref: str | None = None
    category: str | None = None
    body: str | None = None
    confidence: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    raw_payload: dict | None = None


class Storage:
    """SQLite-backed storage for episodes, focus history, and event log."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    # ---- public API -----------------------------------------------------

    def put_episode(self, ep: Episode) -> str:
        """Insert or update (by id) an episode. Returns the episode id."""
        if ep.kind not in VALID_KINDS:
            raise ValueError(f"Invalid episode kind {ep.kind!r}; expected one of {sorted(VALID_KINDS)}")
        ep.updated_at = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO episodes
                   (id, project_id, kind, source_type, source_ref, category,
                    title, body, confidence, created_at, updated_at, raw_payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     kind=excluded.kind, category=excluded.category,
                     title=excluded.title, body=excluded.body,
                     confidence=excluded.confidence, updated_at=excluded.updated_at,
                     raw_payload=excluded.raw_payload""",
                (ep.id, ep.project_id, ep.kind, ep.source_type, ep.source_ref,
                 ep.category, ep.title, ep.body, ep.confidence,
                 ep.created_at, ep.updated_at,
                 json.dumps(ep.raw_payload) if ep.raw_payload is not None else None),
            )
        return ep.id

    def get_episode(self, episode_id: str) -> Episode | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, project_id, kind, source_type, source_ref, category, "
                "title, body, confidence, created_at, updated_at, raw_payload "
                "FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
        return self._row_to_episode(row) if row else None

    def query_episodes(self, project_id: str, *,
                       kind: str | None = None,
                       since: float | None = None,
                       limit: int = 100) -> list[Episode]:
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(f"Invalid kind filter {kind!r}")
        sql = ("SELECT id, project_id, kind, source_type, source_ref, category, "
               "title, body, confidence, created_at, updated_at, raw_payload "
               "FROM episodes WHERE project_id = ?")
        params: list = [project_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def promote_episode(self, episode_id: str, new_kind: str) -> bool:
        """Change an episode's kind (e.g. working → episodic). Returns True if updated."""
        if new_kind not in VALID_KINDS:
            raise ValueError(f"Invalid new kind {new_kind!r}")
        with self._conn() as c:
            cur = c.execute(
                "UPDATE episodes SET kind = ?, updated_at = ? WHERE id = ?",
                (new_kind, time.time(), episode_id),
            )
            return cur.rowcount > 0

    def stats(self, project_id: str | None = None) -> dict:
        sql = "SELECT kind, COUNT(*) FROM episodes"
        params: list = []
        if project_id is not None:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " GROUP BY kind"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return {kind: count for kind, count in rows}

    # ---- event_log raw access (used by EventBus in events.py) ----------

    def append_event(self, *, project_id: str, source_type: str,
                     source_ref: str | None, captured_at: float,
                     payload: dict) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO event_log
                   (project_id, source_type, source_ref, captured_at, payload)
                   VALUES (?,?,?,?,?)""",
                (project_id, source_type, source_ref, captured_at,
                 json.dumps(payload, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def query_events(self, project_id: str, *,
                     source_type: str | None = None,
                     since: float | None = None,
                     limit: int = 100) -> list[dict]:
        sql = ("SELECT id, project_id, source_type, source_ref, captured_at, payload "
               "FROM event_log WHERE project_id = ?")
        params: list = [project_id]
        if source_type is not None:
            sql += " AND source_type = ?"
            params.append(source_type)
        if since is not None:
            sql += " AND captured_at >= ?"
            params.append(since)
        sql += " ORDER BY captured_at DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [
            {
                "id": r[0], "project_id": r[1], "source_type": r[2],
                "source_ref": r[3], "captured_at": r[4],
                "payload": json.loads(r[5]),
            }
            for r in rows
        ]

    def has_event(self, project_id: str, *, source_type: str,
                  source_ref: str | None) -> bool:
        """Return whether an event marker already exists for this source."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM event_log "
                "WHERE project_id = ? AND source_type = ? AND source_ref IS ? "
                "LIMIT 1",
                (project_id, source_type, source_ref),
            ).fetchone()
        return row is not None

    # ---- internals ------------------------------------------------------

    def _bootstrap(self) -> None:
        with self._conn() as c:
            for stmt in _SCHEMA_SQL:
                c.execute(stmt)
            # Ensure schema version is recorded.
            row = c.execute("SELECT version FROM schema_version").fetchone()
            current = row[0] if row else None
            if current is None:
                c.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif current != SCHEMA_VERSION:
                # Fase 0 only knows v1. Refuse to silently upgrade.
                raise RuntimeError(
                    f"DB schema version mismatch: DB has v{current}, code expects v{SCHEMA_VERSION}. "
                    "Migration scripts are Fase 1+."
                )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    @staticmethod
    def _row_to_episode(row: sqlite3.Row) -> Episode:
        raw = row["raw_payload"]
        return Episode(
            id=row["id"],
            project_id=row["project_id"],
            kind=row["kind"],
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            category=row["category"],
            title=row["title"],
            body=row["body"],
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            raw_payload=json.loads(raw) if raw else None,
        )
