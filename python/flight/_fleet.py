from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._read import read

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crashes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL,
    exc_type     TEXT,
    message      TEXT,
    service      TEXT,
    commit_sha   TEXT,
    trace_id     TEXT,
    path         TEXT,
    created_ms   INTEGER,
    ingested_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fp ON crashes(fingerprint);
CREATE INDEX IF NOT EXISTS idx_trace ON crashes(trace_id);
CREATE TABLE IF NOT EXISTS deploys (
    commit_sha TEXT PRIMARY KEY,
    when_ms    INTEGER,
    label      TEXT
);
"""


@dataclass
class Record:
    fingerprint: str
    exc_type: str
    message: str
    service: Optional[str]
    commit: Optional[str]
    trace_id: Optional[str]
    created_ms: int

    def as_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "exc_type": self.exc_type,
            "message": self.message,
            "service": self.service,
            "commit": self.commit,
            "trace_id": self.trace_id,
            "created_ms": self.created_ms,
        }


def extract_record(flight_path) -> Optional[Record]:
    from ._bisect import commit_of
    from ._fingerprint import fingerprint

    try:
        fl = read(flight_path)
    except Exception:
        return None
    if not fl.has_crash:
        return None
    try:
        fp = fingerprint(flight_path)
    except Exception:
        return None
    exc_type, message = ("", "")
    if fl.exceptions:
        exc_type, message = fl.exceptions[0][0], fl.exceptions[0][1]
    ctx = None
    try:
        ctx = fl.correlation()
    except Exception:
        ctx = None
    return Record(
        fingerprint=fp,
        exc_type=exc_type,
        message=message,
        service=(ctx.service if ctx else None),
        commit=commit_of(fl),
        trace_id=(ctx.trace_id if ctx else None),
        created_ms=fl.created_unix_ms,
    )


@dataclass
class Group:

    fingerprint: str
    exc_type: str
    message: str
    count: int
    first_ms: int
    last_ms: int
    services: list[str]
    first_commit: Optional[str] = None
    since_deploy: Optional[str] = None
    is_new: bool = False


class FleetIndex:

    def __init__(self, store, *, index: Optional[str] = None):
        self.store = Path(store)
        self.blobs = self.store / "blobs"
        self.store.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)
        db = index or str(self.store / "index.sqlite")
        if db.startswith("sqlite://"):
            db = db[len("sqlite://") :]
        self._db_path = db
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn


    def ingest_bytes(self, data: bytes, *, filename: Optional[str] = None) -> Optional[Record]:
        name = filename or f"crash-{int(time.time() * 1000)}-{os.getpid()}.flight"
        blob = self.blobs / Path(name).name
        blob.write_bytes(data)
        return self.ingest_path(blob)

    def ingest_path(self, flight_path) -> Optional[Record]:
        rec = extract_record(flight_path)
        if rec is None:
            return None
        with self._conn() as c:
            c.execute(
                "INSERT INTO crashes(fingerprint, exc_type, message, service, commit_sha,"
                " trace_id, path, created_ms, ingested_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rec.fingerprint, rec.exc_type, rec.message, rec.service, rec.commit,
                    rec.trace_id, str(flight_path), rec.created_ms, int(time.time() * 1000),
                ),
            )
        return rec

    def add_deploy(self, commit: str, when_ms: Optional[int] = None, label: str = "") -> None:
        when = when_ms if when_ms is not None else int(time.time() * 1000)
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO deploys(commit_sha, when_ms, label) VALUES (?,?,?)",
                (commit, when, label),
            )


    def top_fingerprints(self, *, window_days: Optional[float] = None, limit: int = 50) -> list[Group]:
        sql = (
            "SELECT fingerprint, COUNT(*) n, MIN(created_ms) first_ms, MAX(created_ms) last_ms "
            "FROM crashes"
        )
        params: list = []
        if window_days is not None:
            cutoff = int(time.time() * 1000) - int(window_days * 86_400_000)
            sql += " WHERE ingested_ms >= ?"
            params.append(cutoff)
        sql += " GROUP BY fingerprint ORDER BY n DESC, last_ms DESC LIMIT ?"
        params.append(limit)
        groups = []
        with self._conn() as c:
            for row in c.execute(sql, params).fetchall():
                groups.append(self._hydrate(c, row))
        return groups

    def _hydrate(self, c, row) -> Group:
        fp = row["fingerprint"]
        detail = c.execute(
            "SELECT exc_type, message, service, commit_sha, created_ms FROM crashes "
            "WHERE fingerprint=? ORDER BY created_ms ASC",
            (fp,),
        ).fetchall()
        services = list(dict.fromkeys(d["service"] for d in detail if d["service"]))
        first = detail[0] if detail else None
        return Group(
            fingerprint=fp,
            exc_type=first["exc_type"] if first else "",
            message=first["message"] if first else "",
            count=row["n"],
            first_ms=row["first_ms"] or 0,
            last_ms=row["last_ms"] or 0,
            services=services,
            first_commit=(first["commit_sha"] if first else None),
        )

    def occurrences(self, fingerprint: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM crashes WHERE fingerprint LIKE ? ORDER BY created_ms ASC",
                (fingerprint + "%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def regressions(self) -> list[Group]:
        with self._conn() as c:
            deploys = c.execute(
                "SELECT commit_sha, when_ms, label FROM deploys ORDER BY when_ms ASC"
            ).fetchall()
        groups = self.top_fingerprints()
        if not deploys:
            return groups
        latest = deploys[-1]
        for g in groups:
            prior = [d for d in deploys if d["when_ms"] <= g.first_ms]
            if prior:
                d = prior[-1]
                g.since_deploy = d["label"] or d["commit_sha"][:12]
            g.is_new = g.first_ms >= latest["when_ms"]
        return groups

    def trace_graph(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        with self._conn() as c:
            rows = c.execute(
                "SELECT trace_id, service, exc_type, message, path FROM crashes "
                "WHERE trace_id IS NOT NULL AND trace_id != '' ORDER BY created_ms ASC"
            ).fetchall()
        for r in rows:
            out.setdefault(r["trace_id"], []).append(dict(r))
        return out

    def stats(self) -> dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM crashes").fetchone()["n"]
            distinct = c.execute("SELECT COUNT(DISTINCT fingerprint) n FROM crashes").fetchone()["n"]
        return {"total": total, "distinct": distinct}


_SECRET_PATTERNS = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer/jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("hex-secret", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
]


def safe_to_send(flight_path) -> tuple[bool, list[str]]:
    concerns: list[str] = []
    try:
        fl = read(flight_path)
    except Exception:
        return False, ["file could not be read"]
    haystack: list[str] = []
    if fl.has_crash:
        try:
            crash = fl.crash()
            for node in crash.objects.values():
                rep = node.get("repr")
                if isinstance(rep, str):
                    haystack.append(rep)
        except Exception:
            pass
    for label, pat in _SECRET_PATTERNS:
        for text in haystack:
            if pat.search(text):
                concerns.append(f"possible {label} in a captured value")
                break
    return (not concerns), concerns


def report_to(url: str, flight_path, *, strict: bool = False, timeout: float = 5.0) -> bool:
    try:
        data = Path(flight_path).read_bytes()
    except Exception:
        return False
    if strict:
        ok, _concerns = safe_to_send(flight_path)
        if not ok:
            return False
    endpoint = url.rstrip("/") + "/ingest"
    req = urllib.request.Request(
        endpoint, data=data, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-Flight-Filename": Path(flight_path).name},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _dashboard_html(index: FleetIndex) -> str:
    groups = index.regressions()
    traces = index.trace_graph()
    st = index.stats()

    def row(g: Group) -> str:
        new = ' <span class="new">↑ new</span>' if g.is_new else ""
        since = f' since {g.since_deploy}' if g.since_deploy else ""
        svc = ", ".join(g.services) or "—"
        return (
            f"<tr><td><code>{g.fingerprint}</code></td><td>{_h(g.exc_type)}: {_h(g.message)}</td>"
            f"<td class='n'>{g.count}×{new}{since}</td><td>{_h(svc)}</td></tr>"
        )

    trace_rows = ""
    for tid, nodes in traces.items():
        svcs = " → ".join(n["service"] or "?" for n in nodes)
        trace_rows += f"<tr><td><code>{tid[:16]}…</code></td><td>{_h(svcs)}</td><td class='n'>{len(nodes)}</td></tr>"

    return f"""<!doctype html><meta charset=utf-8><title>flight fleet</title>
<style>
  :root{{color-scheme:light dark}}
  body{{font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:24px;
        background:Canvas;color:CanvasText}}
  h1{{font-size:18px}} h2{{font-size:13px;text-transform:uppercase;letter-spacing:.04em;opacity:.7}}
  table{{width:100%;border-collapse:collapse;margin-bottom:24px}}
  td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #8884}}
  .n{{white-space:nowrap}} .new{{color:#d29922;font-weight:700}}
  code{{background:#8881;padding:1px 4px;border-radius:4px}}
</style>
<h1>✈ flight fleet — {st['total']} crashes, {st['distinct']} distinct</h1>
<h2>Top fingerprints</h2>
<table><tr><th>fingerprint</th><th>error</th><th>count</th><th>services</th></tr>
{''.join(row(g) for g in groups) or '<tr><td colspan=4>no crashes ingested yet</td></tr>'}</table>
<h2>Cross-service traces</h2>
<table><tr><th>trace</th><th>path</th><th>services</th></tr>
{trace_rows or '<tr><td colspan=3>no correlated traces</td></tr>'}</table>
"""


def _h(s) -> str:
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def make_server(index: FleetIndex, host: str = "127.0.0.1", port: int = 8080):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.rstrip("/") != "/ingest":
                self._send(404, b'{"error":"not found"}')
                return
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length) if length else b""
            filename = self.headers.get("X-Flight-Filename")
            rec = index.ingest_bytes(data, filename=filename)
            if rec is None:
                self._send(422, b'{"error":"no crash to index"}')
                return
            self._send(200, json.dumps(rec.as_dict()).encode())

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/":
                self._send(200, _dashboard_html(index).encode(), "text/html; charset=utf-8")
            elif path == "/api/top":
                body = json.dumps([_group_json(g) for g in index.regressions()]).encode()
                self._send(200, body)
            elif path == "/api/traces":
                self._send(200, json.dumps(index.trace_graph()).encode())
            elif path == "/healthz":
                self._send(200, b'{"ok":true}')
            else:
                self._send(404, b'{"error":"not found"}')

    return ThreadingHTTPServer((host, port), Handler)


def _group_json(g: Group) -> dict:
    return {
        "fingerprint": g.fingerprint,
        "exc_type": g.exc_type,
        "message": g.message,
        "count": g.count,
        "first_ms": g.first_ms,
        "last_ms": g.last_ms,
        "services": g.services,
        "first_commit": g.first_commit,
        "since_deploy": g.since_deploy,
        "is_new": g.is_new,
    }


def serve(store, *, host: str = "127.0.0.1", port: int = 8080, index: Optional[str] = None):
    idx = FleetIndex(store, index=index)
    server = make_server(idx, host, port)
    print(f"[flight] fleet collector on http://{host}:{port}  (store: {store})")
    print("  POST /ingest  ·  GET /  ·  GET /api/top  ·  GET /api/traces")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
