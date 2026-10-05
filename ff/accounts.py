"""Subscription rotation: a pool of Claude Code and Codex logins in ~/.formal-agents.

Each login lives in its own config directory, the one the CLI is told to use:

    ~/.formal-agents/claude/<name>/   CLAUDE_CONFIG_DIR for Claude Code
    ~/.formal-agents/codex/<name>/    CODEX_HOME for Codex
    ~/.formal-agents/accounts.db      the registry (shared by every factory on the machine)

`ff add_login claude|codex` creates the directory and runs the CLI's own login flow in it. When an
agent starts, the runner takes the least recently used account of that kind that is not cooling
down. When the CLI reports a usage limit, the account cools down until the reset time it reports
(or `default_cooldown`), and the run moves to the next account: its session file is copied over and
resumed there, so the agent keeps its context.

Set FF_AGENTS_HOME to use another directory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

KINDS = ("claude", "codex")
DEFAULT_COOLDOWN = 3600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  name TEXT PRIMARY KEY, kind TEXT, dir TEXT, email TEXT, plan TEXT, added REAL, disabled INTEGER DEFAULT 0,
  max_parallel INTEGER DEFAULT 0, cooldown_until REAL DEFAULT 0, cooldown_reason TEXT, last_used REAL DEFAULT 0,
  active INTEGER DEFAULT 0, runs INTEGER DEFAULT 0, limit_hits INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
  output_tokens INTEGER DEFAULT 0);
"""

# What the CLIs say when a subscription runs out. The first group, when present, is the reset time.
LIMIT_PATTERNS = [
    re.compile(r"usage limit reached\|(\d{9,})", re.I),                 # Claude Code: "...|<epoch>"
    re.compile(r"(?:limit reached|limit will reset|usage limit)[^\n]{0,40}?resets? (?:at )?([0-9:]+\s*[ap]m)", re.I),
    re.compile(r"(?:usage limit|rate limit)[^\n]{0,200}?try again in ([0-9][^.\n\"]{0,40})", re.I),  # Codex
    re.compile(r"(?:usage limit|rate limit)[^\n]{0,200}?try again at ([^.\n\"]{3,40})", re.I),
    re.compile(r"(claude ai usage limit reached|you've hit your usage limit|hit your usage limit|"
               r"5-hour limit reached|weekly limit reached|out of extra usage|usage_limit_reached|"
               r"rate_limit_error|\b429\b.{0,40}(?:rate|limit))", re.I),
]


def home() -> Path:
    return Path(os.environ.get("FF_AGENTS_HOME", Path.home() / ".formal-agents"))


def parse_reset(text: str, now: float | None = None) -> float | None:
    """The reset time a limit message announces, as a unix time; None if it names none."""
    now = now or time.time()
    for p in LIMIT_PATTERNS[:4]:
        m = p.search(text)
        if not m:
            continue
        g = m.group(1).strip()
        if g.isdigit():
            return float(g)
        if p is LIMIT_PATTERNS[2]:  # "2 hours 13 minutes", "45 minutes", "1h 5m"
            secs = 0.0
            for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(d|h|m|s)[a-z]*", g, re.I):
                secs += float(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit.lower()]
            if secs:
                return now + secs
            continue
        for fmt in ("%I:%M %p", "%I %p", "%I%p", "%I:%M%p", "%H:%M"):
            try:
                t = datetime.strptime(g.upper().replace(" ", " "), fmt)
            except ValueError:
                continue
            d = datetime.fromtimestamp(now).replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if d.timestamp() <= now:
                d += timedelta(days=1)
            return d.timestamp()
    return None


def is_limit(text: str) -> bool:
    return any(p.search(text) for p in LIMIT_PATTERNS)


@dataclass
class Account:
    name: str
    kind: str
    dir: Path

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": str(self.dir)} if self.kind == "claude" else {"CODEX_HOME": str(self.dir)}


class Pool:
    def __init__(self, root: Path | None = None):
        self.root = root or home()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db().executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.root / "accounts.db", timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _q(self, sql, args=()):
        with self._db() as c:
            return list(c.execute(sql, tuple(args)))

    # ---------------------------------------------------------------- registry

    def list(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self._q("SELECT * FROM accounts WHERE kind = ? ORDER BY name", (kind,))
        return self._q("SELECT * FROM accounts ORDER BY kind, name")

    def get(self, name: str) -> sqlite3.Row | None:
        r = self._q("SELECT * FROM accounts WHERE name = ?", (name,))
        return r[0] if r else None

    def add(self, kind: str, name: str, max_parallel: int = 0) -> Path:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if self.get(name):
            raise ValueError(f"an account named '{name}' already exists")
        d = self.root / kind / name
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        # carry over the user's settings (never credentials) so every account behaves the same
        src = {"claude": Path.home() / ".claude" / "settings.json", "codex": Path.home() / ".codex" / "config.toml"}[kind]
        if src.is_file() and not (d / src.name).exists():
            shutil.copy(src, d / src.name)
        self._q("INSERT INTO accounts (name, kind, dir, added, max_parallel) VALUES (?,?,?,?,?)",
                (name, kind, str(d), time.time(), max_parallel))
        return d

    def remove(self, name: str, delete_dir: bool = False) -> None:
        r = self.get(name)
        if not r:
            raise ValueError(f"no account named '{name}'")
        self._q("DELETE FROM accounts WHERE name = ?", (name,))
        if delete_dir:
            shutil.rmtree(r["dir"], ignore_errors=True)

    def set(self, name: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._q(f"UPDATE accounts SET {sets} WHERE name = ?", list(fields.values()) + [name])

    def status(self, name: str) -> dict:
        """Ask the CLI itself whether this account is logged in (and as whom)."""
        r = self.get(name)
        acct = Account(r["name"], r["kind"], Path(r["dir"]))
        env = dict(os.environ, **acct.env())
        if acct.kind == "claude":
            p = subprocess.run(["claude", "auth", "status", "--json"], env=env, capture_output=True, text=True, timeout=60)
            try:
                d = json.loads(p.stdout)
            except json.JSONDecodeError:
                return {"logged_in": False, "detail": (p.stdout + p.stderr).strip()[:300]}
            return {"logged_in": bool(d.get("loggedIn")), "email": d.get("email"), "plan": d.get("subscriptionType"),
                    "detail": d.get("authMethod")}
        p = subprocess.run(["codex", "login", "status"], env=env, capture_output=True, text=True, timeout=60)
        out = (p.stdout + p.stderr).strip()
        return {"logged_in": p.returncode == 0 and "not logged in" not in out.lower(), "detail": out.splitlines()[-1] if out else ""}

    # ---------------------------------------------------------------- rotation

    def acquire(self, kind: str, allowed: list[str] | None = None, wait: bool = True,
                stop=lambda: False, on_wait=None, strategy: str = "round_robin") -> Account | None:
        """A usable account of `kind`; waits while all are cooling down. None when the pool has no
        account of that kind at all (or `stop()` turned true while waiting).

        round_robin: the least recently used one, so work rotates through every subscription;
        fill_first:  the first in name order that is not cooling down, so one subscription is used
                     until it runs out, then the next, and so on around the list."""
        announced = False
        while True:
            with self._lock:
                rows = [r for r in self.list(kind) if not r["disabled"] and (not allowed or r["name"] in allowed)]
                if not rows:
                    return None
                now = time.time()
                usable = [r for r in rows if r["cooldown_until"] <= now
                          and (not r["max_parallel"] or r["active"] < r["max_parallel"])]
                if usable:
                    if strategy == "fill_first":
                        r = usable[0]
                    else:
                        r = min(usable, key=lambda r: (r["active"], r["last_used"]))
                    self._q("UPDATE accounts SET active = active + 1, last_used = ?, runs = runs + 1 WHERE name = ?",
                            (now, r["name"]))
                    return Account(r["name"], r["kind"], Path(r["dir"]))
                soonest = min(rows, key=lambda r: max(r["cooldown_until"], now))
            if not wait:
                return None
            if on_wait and not announced:
                on_wait(soonest["name"], soonest["cooldown_until"])
                announced = True
            for _ in range(30):
                if stop():
                    return None
                time.sleep(2)

    def release(self, acct: Account, usage: dict | None = None) -> None:
        u = usage or {}
        self._q("UPDATE accounts SET active = MAX(0, active - 1), cost_usd = cost_usd + ?, "
                "output_tokens = output_tokens + ? WHERE name = ?",
                (float(u.get("cost_usd") or 0), int(u.get("output_tokens") or 0), acct.name))

    def cooldown(self, acct: Account, until: float | None, reason: str, default: float = DEFAULT_COOLDOWN) -> float:
        until = until if until and until > time.time() else time.time() + default
        self._q("UPDATE accounts SET cooldown_until = ?, cooldown_reason = ?, limit_hits = limit_hits + 1 WHERE name = ?",
                (until, reason[:300], acct.name))
        return until

    # ---------------------------------------------------------------- moving a session between accounts

    @staticmethod
    def move_session(src: Account, dst: Account, session_id: str) -> bool:
        """Copy the session's files from one account's config dir to another's, at the same
        relative path, so `--resume` / `exec resume` finds it there."""
        moved = False
        for f in src.dir.rglob(f"*{session_id}*"):
            if f.is_file():
                target = dst.dir / f.relative_to(src.dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                moved = True
        return moved


def login(pool: Pool, kind: str, name: str, extra_args: list[str] | None = None, max_parallel: int = 0) -> int:
    """Create the account directory and run the CLI's own interactive login in it."""
    d = pool.add(kind, name, max_parallel)
    env = dict(os.environ)
    env.update(Account(name, kind, d).env())
    argv = (["claude", "auth", "login"] if kind == "claude" else ["codex", "login"]) + (extra_args or [])
    print(f"logging in account '{name}' ({kind}) with its own config dir {d}\n$ {' '.join(argv)}\n", flush=True)
    code = subprocess.call(argv, env=env)
    st = pool.status(name)
    if not st.get("logged_in"):
        print(f"\nnot logged in ({st.get('detail', '')}); the account stays registered: retry with "
              f"`ff relogin {name}` or remove it with `ff remove_login {name}`")
        return code or 1
    pool.set(name, email=st.get("email"), plan=st.get("plan"))
    print(f"\naccount '{name}' ready: {st.get('email') or st.get('detail') or ''} {st.get('plan') or ''}".rstrip())
    return 0
