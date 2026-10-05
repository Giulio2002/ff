"""The factory's memory: one sqlite file every loop writes and the coordinator reads.

Tables: events (the log the coordinator summarises), runs (one per agent process), gates,
findings, rounds, experiments (optimizer history), briefs (coordinator -> loops), decisions
(questions for the human), backlog (implementation items). WAL mode, one connection per
thread.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL, loop TEXT, kind TEXT, run_id TEXT, message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, parent TEXT, loop TEXT, role TEXT, provider TEXT, model TEXT, task TEXT,
  status TEXT, branch TEXT, worktree TEXT, transcript TEXT, attempt INTEGER, started REAL,
  ended REAL, summary TEXT, result TEXT, usage TEXT, pid INTEGER, account TEXT);
CREATE TABLE IF NOT EXISTS gates (
  id INTEGER PRIMARY KEY, run_id TEXT, branch TEXT, commit_sha TEXT, status TEXT, reason TEXT,
  log TEXT, started REAL, ended REAL, durations TEXT);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY, round INTEGER, flavor TEXT, run_id TEXT, title TEXT, severity TEXT,
  description TEXT, reproducer TEXT, status TEXT, verdict TEXT, fix_run TEXT, created REAL);
CREATE TABLE IF NOT EXISTS rounds (
  n INTEGER PRIMARY KEY, started REAL, ended REAL, base_commit TEXT, counts TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY, run_id TEXT, idea TEXT, baseline REAL, candidate REAL, kept INTEGER,
  reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY, loop TEXT, text TEXT, status TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, question TEXT, options TEXT, answer TEXT, asked REAL, answered REAL);
CREATE TABLE IF NOT EXISTS backlog (
  item TEXT PRIMARY KEY, status TEXT, attempts INTEGER, run_id TEXT, updated REAL, note TEXT);
CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, run_id TEXT, sender TEXT, text TEXT, ts REAL, delivered REAL, via TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "account" not in cols:
            self.db.execute("ALTER TABLE runs ADD COLUMN account TEXT")

    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.db.execute(sql, tuple(args)))

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        cur = self.db.execute(sql, tuple(args))
        return cur.lastrowid

    # ---------------------------------------------------------------- events

    def event(self, loop: str, kind: str, message: str, run_id: str | None = None, **data: Any) -> None:
        self.x("INSERT INTO events (ts, loop, kind, run_id, message, data) VALUES (?,?,?,?,?,?)",
               (time.time(), loop, kind, run_id, message, json.dumps(data, default=str) if data else None))

    def events(self, since: float = 0, loop: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if loop:
            return self.q("SELECT * FROM events WHERE ts >= ? AND loop = ? ORDER BY id DESC LIMIT ?",
                          (since, loop, limit))[::-1]
        return self.q("SELECT * FROM events WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit))[::-1]

    # ---------------------------------------------------------------- runs

    def run_start(self, run_id: str, **f: Any) -> None:
        f.setdefault("status", "running")
        f.setdefault("started", time.time())
        cols = ["id"] + list(f)
        self.x(f"INSERT INTO runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
               [run_id] + [json.dumps(v) if isinstance(v, (dict, list)) else v for v in f.values()])

    def run_update(self, run_id: str, **f: Any) -> None:
        if not f:
            return
        sets = ", ".join(f"{k} = ?" for k in f)
        self.x(f"UPDATE runs SET {sets} WHERE id = ?",
               [json.dumps(v) if isinstance(v, (dict, list)) else v for v in f.values()] + [run_id])

    def run(self, run_id: str) -> sqlite3.Row | None:
        r = self.q("SELECT * FROM runs WHERE id = ?", (run_id,))
        return r[0] if r else None

    def children(self, run_id: str, recursive: bool = False) -> list[sqlite3.Row]:
        kids = self.q("SELECT * FROM runs WHERE parent = ? ORDER BY started", (run_id,))
        if not recursive:
            return kids
        out = []
        for k in kids:
            out.append(k)
            out += self.children(k["id"], True)
        return out

    # ---------------------------------------------------------------- steering messages

    def send(self, run_id: str, text: str, sender: str = "api") -> int:
        mid = self.x("INSERT INTO messages (run_id, sender, text, ts) VALUES (?,?,?,?)",
                     (run_id, sender, text, time.time()))
        self.event("steer", "message", f"{sender} -> {run_id}: {text[:200]}", run_id)
        return mid

    def pending(self, run_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM messages WHERE run_id = ? AND delivered IS NULL ORDER BY id", (run_id,))

    def mark_delivered(self, ids: list[int], via: str) -> None:
        for i in ids:
            self.x("UPDATE messages SET delivered = ?, via = ? WHERE id = ?", (time.time(), via, i))

    # ---------------------------------------------------------------- flags (pause/resume, round state)

    def flag(self, key: str, default: str | None = None) -> str | None:
        r = self.q("SELECT value FROM flags WHERE key = ?", (key,))
        return r[0]["value"] if r else default

    def set_flag(self, key: str, value: str | None) -> None:
        if value is None:
            self.x("DELETE FROM flags WHERE key = ?", (key,))
        else:
            self.x("INSERT INTO flags (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, value))

    def paused(self, loop: str) -> bool:
        return self.flag(f"paused:{loop}") == "1" or self.flag("paused:all") == "1"

    # ---------------------------------------------------------------- briefs and decisions

    def take_briefs(self, loop: str) -> list[str]:
        rows = self.q("SELECT id, text FROM briefs WHERE status = 'open' AND loop IN (?, 'all') ORDER BY id", (loop,))
        for r in rows:
            self.x("UPDATE briefs SET status = 'delivered' WHERE id = ?", (r["id"],))
        return [r["text"] for r in rows]

    def ask(self, question: str, options: list[str]) -> int:
        open_ = self.q("SELECT id FROM decisions WHERE question = ? AND answer IS NULL", (question,))
        if open_:
            return open_[0]["id"]
        did = self.x("INSERT INTO decisions (question, options, asked) VALUES (?,?,?)",
                     (question, json.dumps(options), time.time()))
        self.event("factory", "decision", f"waiting for a decision #{did}: {question} {options}")
        return did

    def answer(self, did: int) -> str | None:
        r = self.q("SELECT answer FROM decisions WHERE id = ?", (did,))
        return r[0]["answer"] if r else None
