"""HTTP API to steer the factory: start an agent, message it (and through it, its subagents),
stop it, and everything the coordinator can do from the CLI.

All requests need `Authorization: Bearer <token>` (FF_API_TOKEN, or the token printed at start).
Binds to 127.0.0.1 by default.

  GET  /status                           loops, running agents, gate, decisions, cost
  GET  /events?since=<unix>&loop=<l>     the event log
  GET  /runs?status=running              runs
  GET  /runs/<id>                        one run: result, usage, children, messages, transcript tail
  GET  /runs/<id>/tree                   the run and all its descendants
  POST /agents        {"role", "task", "parent"?}            start an agent -> {"run_id"}
  POST /runs/<id>/steer {"message", "cascade"?: false}       message a run (and its live subagents)
  POST /runs/<id>/stop  {"cascade"?: true}
  POST /briefs        {"loop", "text"}
  GET  /decisions ;  POST /decisions/<id> {"answer"}
  POST /loops/<name>/pause ; POST /loops/<name>/resume       (name may be "all")
"""
from __future__ import annotations

import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .factory import Factory


def _rows(rows):
    return [dict(r) for r in rows]


def make_handler(f: Factory, token: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, default=str, indent=1).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> bool:
            if self.headers.get("authorization", "") == f"Bearer {token}":
                return True
            self._send(401, {"error": "missing or wrong bearer token"})
            return False

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            parts = [p for p in u.path.split("/") if p]
            s = f.store
            if parts == ["status"]:
                return self._send(200, f.status())
            if parts == ["events"]:
                return self._send(200, _rows(s.events(float(q.get("since", time.time() - 86400)), q.get("loop"),
                                                      int(q.get("limit", 500)))))
            if parts == ["runs"]:
                if "status" in q:
                    return self._send(200, _rows(s.q("SELECT id, parent, loop, role, provider, model, status, started, ended, "
                                                     "summary FROM runs WHERE status = ? ORDER BY started DESC LIMIT 200",
                                                     (q["status"],))))
                return self._send(200, _rows(s.q("SELECT id, parent, loop, role, provider, model, status, started, ended, "
                                                 "summary FROM runs ORDER BY started DESC LIMIT 200")))
            if len(parts) == 2 and parts[0] == "runs":
                v = f.run_view(parts[1])
                return self._send(200, v) if v else self._send(404, {"error": "no such run"})
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "tree":
                root = s.run(parts[1])
                if not root:
                    return self._send(404, {"error": "no such run"})
                return self._send(200, [dict(id=r["id"], parent=r["parent"], role=r["role"], status=r["status"])
                                        for r in [root] + s.children(parts[1], True)])
            if parts == ["decisions"]:
                return self._send(200, _rows(s.q("SELECT * FROM decisions WHERE answer IS NULL")))
            if parts == ["findings"]:
                return self._send(200, _rows(s.q("SELECT id, round, flavor, title, severity, status FROM findings "
                                                 "ORDER BY id DESC LIMIT 500")))
            return self._send(404, {"error": "unknown endpoint"})

        def do_POST(self):
            if not self._auth():
                return
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                b = self._body()
            except json.JSONDecodeError:
                return self._send(400, {"error": "body must be JSON"})
            s = f.store
            try:
                if parts == ["agents"]:
                    role, task = b.get("role"), b.get("task")
                    if not role or not task:
                        return self._send(400, {"error": "role and task are required"})
                    f.cfg.role(role)
                    rid = f.start_agent(role, task, parent=b.get("parent"))
                    return self._send(201, {"run_id": rid})
                if len(parts) == 3 and parts[0] == "runs" and parts[2] in ("steer", "stop"):
                    if not s.run(parts[1]):
                        return self._send(404, {"error": "no such run"})
                    if parts[2] == "steer":
                        if not b.get("message"):
                            return self._send(400, {"error": "message is required"})
                        ids = f.runner.steer(parts[1], b["message"], sender=b.get("sender", "api"),
                                             cascade=bool(b.get("cascade")))
                        return self._send(200, {"delivered_to": ids})
                    return self._send(200, {"stopping": f.runner.stop(parts[1], bool(b.get("cascade", True)))})
                if parts == ["briefs"]:
                    bid = s.x("INSERT INTO briefs (loop, text, status, ts) VALUES (?,?, 'open', ?)",
                              (b["loop"], b["text"], time.time()))
                    s.event(b["loop"], "brief", f"brief #{bid}: {b['text'][:200]}")
                    return self._send(201, {"brief": bid})
                if len(parts) == 2 and parts[0] == "decisions":
                    s.x("UPDATE decisions SET answer = ?, answered = ? WHERE id = ?", (b["answer"], time.time(), int(parts[1])))
                    s.event("factory", "decided", f"decision #{parts[1]}: {b['answer']}")
                    return self._send(200, {"ok": True})
                if len(parts) == 3 and parts[0] == "loops" and parts[2] in ("pause", "resume"):
                    s.set_flag(f"paused:{parts[1]}", "1" if parts[2] == "pause" else None)
                    s.event(parts[1], parts[2], f"loop {parts[1]} {parts[2]}d via API")
                    return self._send(200, {"ok": True})
            except KeyError as e:
                return self._send(400, {"error": f"missing field {e}"})
            except Exception as e:
                return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "unknown endpoint"})

    return H


def serve(f: Factory, host: str = "127.0.0.1", port: int = 8787, token: str | None = None) -> ThreadingHTTPServer:
    token = token or os.environ.get("FF_API_TOKEN") or secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer((host, port), make_handler(f, token))
    srv.token = token  # type: ignore[attr-defined]
    return srv
