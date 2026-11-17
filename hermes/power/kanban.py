"""Kanban multi-agent board (#92).

A shared SQLite board (in the *base* home, so every profile sees it). An
orchestrator agent decomposes work into cards; worker agents - optionally
running as different specialist profiles (``hermes -p researcher kanban work``)
- claim cards atomically, run them in isolated sessions and post results.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from hermes import config

STATUSES = ["todo", "doing", "blocked", "review", "done"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    board TEXT DEFAULT 'default',
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    status TEXT DEFAULT 'todo',
    assignee TEXT DEFAULT '',
    parent_id INTEGER,
    depends_on TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    claimed_by TEXT DEFAULT '',
    result TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL
);
"""


class Board:
    def __init__(self, path: Path | None = None, board: str = "default"):
        self.path = Path(path or config.base_home() / "kanban.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.board = board
        self.lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        """Short-lived connection: commit on success, always closed."""
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, title: str, body: str = "", assignee: str = "", parent_id: int | None = None,
            depends_on: list[int] | None = None, created_by: str = "") -> dict:
        now = time.time()
        with self.lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO cards(board,title,body,assignee,parent_id,depends_on,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (self.board, title, body, assignee, parent_id, ",".join(map(str, depends_on or [])), created_by, now, now))
            return self.get(cur.lastrowid, c)

    def get(self, card_id: int, conn: sqlite3.Connection | None = None) -> dict:
        if conn is None:
            with self._conn() as c:
                return self.get(card_id, c)
        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        return dict(row) if row else {}

    def list(self, status: str | None = None, assignee: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM cards WHERE board=?", [self.board]
        if status:
            q += " AND status=?"
            args.append(status)
        if assignee is not None:
            q += " AND assignee=?"
            args.append(assignee)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY id", args)]

    def move(self, card_id: int, status: str, result: str | None = None) -> dict:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with self.lock, self._conn() as c:
            if result is not None:
                c.execute("UPDATE cards SET status=?, result=?, updated_at=? WHERE id=?", (status, result, time.time(), card_id))
            else:
                c.execute("UPDATE cards SET status=?, updated_at=? WHERE id=?", (status, time.time(), card_id))
            return self.get(card_id, c)

    def comment(self, card_id: int, text: str) -> dict:
        with self.lock, self._conn() as c:
            c.execute("UPDATE cards SET body = body || ? , updated_at=? WHERE id=?", (f"\n\n[note] {text}", time.time(), card_id))
            return self.get(card_id, c)

    def set_session(self, card_id: int, session_id: str) -> None:
        with self.lock, self._conn() as c:
            c.execute("UPDATE cards SET session_id=? WHERE id=?", (session_id, card_id))

    def _deps_done(self, card: dict, c: sqlite3.Connection) -> bool:
        deps = [int(x) for x in (card.get("depends_on") or "").split(",") if x.strip()]
        if not deps:
            return True
        rows = c.execute(f"SELECT status FROM cards WHERE id IN ({','.join('?' * len(deps))})", deps).fetchall()
        return all(r["status"] == "done" for r in rows)

    def claim(self, worker: str, profile: str = "") -> dict | None:
        """Atomically claim the oldest ready card for this profile (or unassigned)."""
        with self.lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT * FROM cards WHERE board=? AND status='todo' AND (assignee='' OR assignee=?) ORDER BY id",
                             (self.board, profile)).fetchall()
            for row in rows:
                card = dict(row)
                if self._deps_done(card, c):
                    c.execute("UPDATE cards SET status='doing', claimed_by=?, updated_at=? WHERE id=? AND status='todo'",
                              (worker, time.time(), card["id"]))
                    c.execute("COMMIT")
                    card.update(status="doing", claimed_by=worker)
                    return card
            c.execute("COMMIT")
            return None

    def context_for(self, card: dict) -> str:
        """Results of dependencies/siblings so workers can build on each other."""
        deps = [int(x) for x in (card.get("depends_on") or "").split(",") if x.strip()]
        parts = []
        for d in deps:
            dep = self.get(d)
            if dep:
                parts.append(f"Result of card #{d} ({dep['title']}):\n{dep['result'][:4000]}")
        return "\n\n".join(parts)


def run_worker(cfg: dict, board: Board, profile: str = "", once: bool = False, poll: float = 5,
               stop: threading.Event | None = None, agent_factory=None) -> int:
    """Claim and execute cards until stopped (or until the queue is empty with once=True)."""
    stop = stop or threading.Event()
    worker = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident() % 10000}"
    done = 0
    if agent_factory is None:
        from hermes.agent import AIAgent

        def agent_factory(**kw):
            return AIAgent(**kw)
    while not stop.is_set():
        card = board.claim(worker, profile)
        if not card:
            if once:
                break
            stop.wait(poll)
            continue
        prompt = (f"You are a worker on a kanban board. Complete card #{card['id']}: {card['title']}\n\n{card['body']}\n\n"
                  + (board.context_for(card) + "\n\n" if card.get("depends_on") else "")
                  + "Work autonomously. End with the deliverable/result. If you are blocked, start your reply with BLOCKED: and say why.")
        try:
            agent = agent_factory(config=cfg, platform="kanban", toolsets=(cfg.get("platform_toolsets", {}) or {}).get("cron", ["hermes-cron"]),
                                  approval_mode="deny-dangerous")
            res = agent.run_conversation(prompt)
            text = res.get("final_response") or ""
            status = "blocked" if text.strip().upper().startswith("BLOCKED") else "done"
            board.move(card["id"], status, text)
            board.set_session(card["id"], getattr(agent, "session_id", ""))
        except Exception as exc:
            board.move(card["id"], "blocked", f"worker error: {exc}")
        done += 1
    return done
