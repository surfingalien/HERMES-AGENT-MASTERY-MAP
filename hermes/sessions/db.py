"""SQLite session store with FTS5 full-text search (#9 sessions, #24 session_search,
#25 cross-session recall, #100 insights & cost tracking)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT,
    user_id TEXT,
    chat_id TEXT,
    model TEXT,
    title TEXT,
    started_at TEXT,
    ended_at TEXT,
    parent_session_id TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    extra TEXT,
    ts REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS tool_usage (
    session_id TEXT, tool_name TEXT, ok INTEGER, duration_ms INTEGER, ts REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content='messages', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
"""


_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]


class SessionDB:
    def __init__(self, db_path: Path | None = None):
        self.path = Path(db_path or config.path("state.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One shared connection, serialised by an RLock (safe across agent/tool threads).
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """Switch to WAL and create tables once per file per process.

        Changing journal mode and running DDL can fail fast with "database is
        locked" when several agents open the same file at once (parallel batch
        workers, delegation), so it is serialised in-process and retried
        across processes.
        """
        key = str(self.path.resolve())
        with _INIT_LOCK:
            if key in _INITIALIZED:
                return
            for attempt in range(20):
                try:
                    self.conn.execute("PRAGMA journal_mode=WAL")
                    with self.conn:
                        self.conn.executescript(SCHEMA)
                    _INITIALIZED.add(key)
                    return
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) and "busy" not in str(exc) or attempt == 19:
                        raise
                    time.sleep(0.05 * (attempt + 1))

    def _conn(self) -> sqlite3.Connection:
        return self.conn

    def _read(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __del__(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- writes
    def create_session(self, session_id: str | None = None, source: str = "cli", model: str = "", user_id: str = "",
                       chat_id: str = "", parent_session_id: str | None = None, title: str = "") -> str:
        sid = session_id or new_session_id()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO sessions(id, source, user_id, chat_id, model, title, started_at, parent_session_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sid, source, user_id, chat_id, model, title, now_iso(), parent_session_id),
            )
        return sid

    def add_message(self, session_id: str, msg: dict) -> None:
        content = msg.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "[image]") if isinstance(p, dict) else str(p) for p in content)
        extra = {k: v for k, v in msg.items() if k.startswith("_")}
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO messages(session_id, role, content, tool_calls, tool_call_id, tool_name, extra, ts) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, msg.get("role"), content or "", json.dumps(msg["tool_calls"]) if msg.get("tool_calls") else None,
                 msg.get("tool_call_id"), msg.get("name"), json.dumps(extra, default=str) if extra else None, time.time()),
            )
            c.execute("UPDATE sessions SET message_count = message_count + 1, tool_call_count = tool_call_count + ? WHERE id=?",
                      (len(msg.get("tool_calls") or []), session_id))

    def add_usage(self, session_id: str, input_tokens: int, output_tokens: int, cache_read: int, cost: float, model: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE sessions SET input_tokens=input_tokens+?, output_tokens=output_tokens+?, cache_read_tokens=cache_read_tokens+?, "
                "cost_usd=cost_usd+?, model=CASE WHEN ?<>'' THEN ? ELSE model END WHERE id=?",
                (input_tokens, output_tokens, cache_read, cost, model, model, session_id),
            )

    def record_tool(self, session_id: str, tool_name: str, ok: bool, duration_ms: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO tool_usage VALUES (?,?,?,?,?)", (session_id, tool_name, int(ok), duration_ms, time.time()))

    def set_title(self, session_id: str, title: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE sessions SET title=? WHERE id=?", (title[:120], session_id))

    def end_session(self, session_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE sessions SET ended_at=? WHERE id=?", (now_iso(), session_id))

    def delete_session(self, session_id: str) -> bool:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            c.execute("DELETE FROM tool_usage WHERE session_id=?", (session_id,))
            return c.execute("DELETE FROM sessions WHERE id=?", (session_id,)).rowcount > 0

    def prune(self, older_than_days: int) -> int:
        cutoff = datetime.fromtimestamp(time.time() - older_than_days * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = [r["id"] for r in self._read("SELECT id FROM sessions WHERE started_at < ?", (cutoff,))]
        for sid in ids:
            self.delete_session(sid)
        return len(ids)

    # ---------------------------------------------------------------- reads
    def get_session(self, session_id: str) -> dict | None:
        rows = self._read("SELECT * FROM sessions WHERE id=?", (session_id,))
        return dict(rows[0]) if rows else None

    def resolve(self, prefix_or_title: str) -> str | None:
        rows = self._read("SELECT id FROM sessions WHERE id=? OR title=? ORDER BY started_at DESC LIMIT 1",
                          (prefix_or_title, prefix_or_title))
        if not rows:
            rows = self._read("SELECT id FROM sessions WHERE id LIKE ? ORDER BY started_at DESC LIMIT 1", (prefix_or_title + "%",))
        return rows[0]["id"] if rows else None

    def latest(self, source: str | None = None) -> str | None:
        q = "SELECT id FROM sessions" + (" WHERE source=?" if source else "") + " ORDER BY started_at DESC, rowid DESC LIMIT 1"
        rows = self._read(q, (source,) if source else ())
        return rows[0]["id"] if rows else None

    def list_sessions(self, limit: int = 20, source: str | None = None) -> list[dict]:
        q = "SELECT * FROM sessions" + (" WHERE source=?" if source else "") + " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        args: tuple[Any, ...] = (source, limit) if source else (limit,)
        return [dict(r) for r in self._read(q, args)]

    def get_messages(self, session_id: str) -> list[dict]:
        out = []
        for r in self._read("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)):
            msg: dict[str, Any] = {"role": r["role"], "content": r["content"]}
            if r["tool_calls"]:
                msg["tool_calls"] = json.loads(r["tool_calls"])
            if r["tool_call_id"]:
                msg["tool_call_id"] = r["tool_call_id"]
            if r["tool_name"]:
                msg["name"] = r["tool_name"]
            if r["extra"]:
                msg.update(json.loads(r["extra"]))
            out.append(msg)
        return out

    def search(self, query: str, limit: int = 10, exclude_session: str = "", role: str | None = None) -> list[dict]:
        """FTS5 search (porter stemming); returns one best hit per session.

        Plain queries match all words first, then fall back to any word.
        """
        if any(op in query for op in ('"', " OR ", " AND ", " NOT ", "*")):
            return self._search(query, limit, exclude_session, role)
        words = ['"' + w.replace('"', "") + '"' for w in query.split() if w.strip()]
        if not words:
            return []
        hits = self._search(" ".join(words), limit, exclude_session, role)
        if not hits and len(words) > 1:
            hits = self._search(" OR ".join(words), limit, exclude_session, role)
        return hits

    def _search(self, fts_query: str, limit: int, exclude_session: str, role: str | None) -> list[dict]:
        sql = (
            "SELECT m.session_id, m.role, snippet(messages_fts, 0, '>>', '<<', '...', 24) AS snippet, s.title, s.started_at, s.source, "
            "bm25(messages_fts) AS rank FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "JOIN sessions s ON s.id = m.session_id WHERE messages_fts MATCH ? AND m.session_id <> ?"
            + (" AND m.role = ?" if role else "") + " ORDER BY rank LIMIT ?"
        )
        args: list[Any] = [fts_query, exclude_session or ""]
        if role:
            args.append(role)
        args.append(limit * 5)
        try:
            rows = self._read(sql, args)
        except sqlite3.OperationalError:
            return []
        seen, out = set(), []
        for r in rows:
            if r["session_id"] in seen:
                continue
            seen.add(r["session_id"])
            out.append({"session_id": r["session_id"], "title": r["title"] or "", "started_at": r["started_at"] or "",
                        "source": r["source"], "role": r["role"], "snippet": r["snippet"]})
            if len(out) >= limit:
                break
        return out

    # ---------------------------------------------------------------- analytics
    def insights(self, days: int = 30) -> dict:
        cutoff = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            return self._insights(cutoff, days)

    def _insights(self, cutoff: str, days: int) -> dict:
        c = self.conn
        totals = dict(c.execute(
            "SELECT COUNT(*) sessions, COALESCE(SUM(message_count),0) messages, COALESCE(SUM(input_tokens),0) input_tokens, "
            "COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(cache_read_tokens),0) cache_read_tokens, "
            "COALESCE(SUM(cost_usd),0) cost_usd, COALESCE(SUM(tool_call_count),0) tool_calls FROM sessions WHERE started_at >= ?",
            (cutoff,)).fetchone())
        by_model = [dict(r) for r in c.execute(
            "SELECT model, COUNT(*) sessions, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, SUM(cost_usd) cost_usd "
            "FROM sessions WHERE started_at >= ? GROUP BY model ORDER BY cost_usd DESC", (cutoff,))]
        by_source = [dict(r) for r in c.execute(
            "SELECT source, COUNT(*) sessions, SUM(cost_usd) cost_usd FROM sessions WHERE started_at >= ? GROUP BY source ORDER BY sessions DESC",
            (cutoff,))]
        tools = [dict(r) for r in c.execute(
            "SELECT tool_name, COUNT(*) calls, SUM(ok) ok, CAST(AVG(duration_ms) AS INTEGER) avg_ms FROM tool_usage "
            "WHERE ts >= ? GROUP BY tool_name ORDER BY calls DESC LIMIT 20", (time.time() - days * 86400,))]
        top = [dict(r) for r in c.execute(
            "SELECT id, title, source, cost_usd, input_tokens + output_tokens tokens FROM sessions WHERE started_at >= ? "
            "ORDER BY cost_usd DESC, tokens DESC LIMIT 5", (cutoff,))]
        return {"days": days, "totals": totals, "by_model": by_model, "by_source": by_source, "tools": tools, "top_sessions": top}
