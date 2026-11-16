#!/usr/bin/env python3
"""Sample Linux TCP sockets every 60 seconds; retain history in SQLite."""

import argparse
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

INTERVAL = 60
DATA_NAME = "tcp_connections.txt"
DB_NAME = "tcp_connections.sqlite3"
STATES = ("established", "all", "time-wait", "close-wait", "syn-recv", "syn-sent")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def port_number(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def sample(ss_path, state, ports, now=None):
    command = [ss_path, "-H", "-t", "-n", "-a", "state", state]
    if ports:
        command.append("( " + " or ".join("sport = :%d" % p for p in ports) + " )")
    record = {"timestamp": int(time.time() if now is None else now),
              "state": state, "ports": list(ports), "count": None,
              "port_counts": {str(port): None for port in ports}}
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=20, check=False)
        if result.returncode:
            record["error"] = "ss_exit_%d" % result.returncode
        else:
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            counts = {str(port): 0 for port in ports}
            try:
                if ports:
                    for line in lines:
                        # Without -p/-e/-i, the final columns are local and peer
                        # endpoints, with or without a leading State column.
                        fields = line.split()
                        if len(fields) < 4:
                            raise ValueError("missing socket columns")
                        local_port = str(int(fields[-2].rsplit(":", 1)[1]))
                        counts[local_port] += 1
                record["count"] = len(lines)
                record["port_counts"] = counts
            except (ValueError, IndexError, KeyError):
                record["error"] = "ss_output_unrecognized"
    except subprocess.TimeoutExpired:
        record["error"] = "ss_timeout"
    except OSError:
        record["error"] = "ss_unavailable"
    return record


class _LegacyHistory:
    def __init__(self, path, days, state, ports, now=None):
        self.path = Path(path)
        self.days, self.state, self.ports = days, state, list(ports)
        self.limit = days * 1440 + 1
        self.rows = deque(maxlen=self.limit)
        current = time.time() if now is None else now
        skipped = 0
        if self.path.exists():
            with self.path.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        row = json.loads(line)
                        timestamp, count = row["timestamp"], row["count"]
                        if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                                or (count is not None and (type(count) is not int or count < 0))):
                            raise ValueError("invalid sample")
                        if not current - days * 86400 <= timestamp <= current:
                            continue
                        if row.get("state") != state:
                            raise RuntimeError("Existing data uses another TCP state; "
                                               "choose a different --output-dir.")
                        row_ports = row.get("ports")
                        if (not isinstance(row_ports, list)
                                or any(type(port) is not int or not 1 <= port <= 65535 for port in row_ports)
                                or len(set(row_ports)) != len(row_ports)):
                            raise ValueError("invalid sample ports")
                        row_ports = sorted(row_ports)
                        clean = {"timestamp": timestamp, "state": state,
                                 "ports": row_ports, "count": count}
                        if "port_counts" in row:
                            counts = row["port_counts"]
                            if not isinstance(counts, dict) or set(counts) != {str(p) for p in row_ports}:
                                raise ValueError("invalid port counts")
                            if count is None:
                                if any(value is not None for value in counts.values()):
                                    raise ValueError("invalid failed sample")
                            elif any(type(value) is not int or value < 0 for value in counts.values()):
                                raise ValueError("invalid port count")
                            elif row_ports and sum(counts.values()) != count:
                                raise ValueError("port counts do not match total")
                            clean["port_counts"] = counts
                        if count is None:
                            clean["error"] = str(row.get("error", "collection_failed"))[:80]
                        self.rows.append(clean)
                    except (ValueError, KeyError, TypeError):
                        skipped += 1
        self.rows = deque(sorted(self.rows, key=lambda row: row["timestamp"]), maxlen=self.limit)
        if skipped:
            print("Ignored %d malformed history lines." % skipped, file=sys.stderr)

class History:
    def __init__(self, path, days, state, ports, now=None):
        self.path = Path(path)
        self.days, self.state, self.ports = days, state, list(ports)
        self.limit = days * 1440 + 1
        current = time.time() if now is None else now
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, record TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS samples_time ON samples(timestamp, id);
                CREATE TABLE IF NOT EXISTS sample_ports (
                    sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
                    port INTEGER NOT NULL, PRIMARY KEY(sample_id, port));
                CREATE INDEX IF NOT EXISTS ports_lookup ON sample_ports(port, sample_id);
            """)
            with db:
                db.execute("BEGIN IMMEDIATE")
                stored_state = db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
                if stored_state and stored_state[0] != state and db.execute(
                        "SELECT 1 FROM samples WHERE timestamp BETWEEN ? AND ? LIMIT 1",
                        (current - days * 86400, current)).fetchone():
                    raise RuntimeError("Existing data uses another TCP state; "
                                       "choose a different --output-dir.")
                if not db.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
                    legacy = _LegacyHistory(self.path.with_name(DATA_NAME), days, state, ports, current)
                    for row in legacy.rows:
                        self._insert(db, row)
                    db.execute("INSERT INTO metadata VALUES ('legacy_imported', '1')")
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('state', ?)", (state,))
                self._prune(db, current)
                self._count = self._cap(db, db.execute("SELECT count(*) FROM samples").fetchone()[0])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    @staticmethod
    def _insert(db, row):
        sample_id = db.execute("INSERT INTO samples(timestamp, record) VALUES (?, ?)",
                               (row["timestamp"], json.dumps(row, separators=(",", ":")))).lastrowid
        db.executemany("INSERT INTO sample_ports VALUES (?, ?)",
                       ((sample_id, port) for port in row["ports"]))

    def _prune(self, db, now):
        deleted = db.execute("DELETE FROM samples WHERE timestamp < ?",
                             (now - self.days * 86400,)).rowcount
        deleted += db.execute("DELETE FROM samples WHERE timestamp > ?", (now,)).rowcount
        return deleted

    def save(self, now, row=None):
        with self.connect() as db, db:
            count = self._count - self._prune(db, now)
            if row is not None:
                self._insert(db, row)
                count += 1
            count = self._cap(db, count)
        self._count = count

    def _cap(self, db, count):
        if count > self.limit:
            db.execute("DELETE FROM samples WHERE id IN "
                       "(SELECT id FROM samples ORDER BY timestamp, id LIMIT ?)",
                       (count - self.limit,))
        return min(count, self.limit)

    def query(self, start=None, end=None):
        with self.connect() as db, db:
            # All fields describe the same committed snapshot, even during a sample.
            db.execute("BEGIN")
            latest = db.execute("SELECT record FROM samples ORDER BY timestamp DESC, id DESC LIMIT 1").fetchone()
            first = db.execute("SELECT timestamp FROM samples ORDER BY timestamp, id LIMIT 1").fetchone()
            clauses, params = [], []
            for name, value, operator in (("timestamp", start, ">="), ("timestamp", end, "<=")):
                if value is not None:
                    clauses.append(name + operator + "?")
                    params.append(value)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            records = [json.loads(r[0]) for r in db.execute(
                "SELECT record FROM samples" + where + " ORDER BY timestamp, id", params)]
            return {"records": records, "latest": json.loads(latest[0]) if latest else None,
                    "first": first[0] if first else None,
                    "ports": [r[0] for r in db.execute("SELECT DISTINCT port FROM sample_ports ORDER BY port")]}

    @property
    def rows(self):
        return self.query()["records"]


def make_server(bind, port, history, html_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_file(False)

        def do_HEAD(self):
            self.send_file(True)

        def send_file(self, head):
            url = urlsplit(self.path)
            route = url.path
            if route == "/api/history":
                try:
                    params = parse_qs(url.query, keep_blank_values=True, max_num_fields=10)
                    if set(params) - {"start", "end"} or any(len(v) != 1 for v in params.values()):
                        raise ValueError("invalid parameters")
                    bounds = {key: float(value[0]) for key, value in params.items()}
                    if any(not math.isfinite(v) for v in bounds.values()):
                        raise ValueError("non-finite timestamp")
                    if bounds.get("start", -math.inf) > bounds.get("end", math.inf):
                        raise ValueError("invalid time range")
                    payload = json.dumps(history.query(**bounds), separators=(",", ":")).encode("utf-8")
                except ValueError:
                    self.send_error(400, "Invalid time range")
                    return
                except sqlite3.Error:
                    self.send_error(503, "History temporarily unavailable")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(payload)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                return
            if route == "/" + DATA_NAME:
                # Export a consistent snapshot without creating another history file.
                try:
                    with history.connect() as db, db:
                        db.execute("BEGIN")
                        cursor = db.execute("SELECT record FROM samples ORDER BY timestamp, id")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.send_header("Content-Disposition", 'attachment; filename="' + DATA_NAME + '"')
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        if not head:
                            for record, in cursor:
                                self.wfile.write((record + "\n").encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if route in ("/", "/index.html"):
                path, mime = html_path, "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            try:
                with Path(path).open("rb") as source:
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(os.fstat(source.fileno()).st_size))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    if not head:
                        shutil.copyfileobj(source, self.wfile)
            except FileNotFoundError:
                self.send_error(404, "File not found")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            # Polling the page must not produce an ever-growing access log.
            pass

    return ThreadingHTTPServer((bind, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=positive_int, default=7, help="retention in days (default: 7)")
    parser.add_argument("--port", type=port_number, action="append", default=[],
                        help="local TCP port; repeat to record individual ports and their total")
    parser.add_argument("--state", choices=STATES, default="established")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--http-port", type=int, default=8765, help="dashboard port; 0 disables HTTP")
    parser.add_argument("--bind", default="127.0.0.1", help="dashboard bind address")
    parser.add_argument("--once", action="store_true", help="collect once and exit, without HTTP")
    args = parser.parse_args(argv)
    if not 0 <= args.http_port <= 65535:
        parser.error("--http-port must be between 0 and 65535")
    if sys.platform != "linux":
        parser.error("Collection requires Linux with iproute2 (ss).")
    ss_path = shutil.which("ss")
    if not ss_path:
        parser.error("ss not found; install iproute2 / iproute first.")
    html_path = Path(__file__).resolve().with_name("index.html")
    if not args.once and args.http_port and not html_path.is_file():
        parser.error("index.html must be beside tcp_monitor.py")

    import fcntl
    args.output_dir.mkdir(parents=True, exist_ok=True)
    lock = (args.output_dir / ".tcp-monitor.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        parser.error("Another monitor is already using this --output-dir.")
    server = None
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    try:
        history = History(args.output_dir / DB_NAME, args.days, args.state, sorted(set(args.port)))
        history.save(time.time())
        if args.http_port and not args.once:
            server = make_server(args.bind, args.http_port, history, html_path)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print("Dashboard: http://%s:%d/" % (args.bind, args.http_port), flush=True)
        print("TCP state=%s local_ports=%s retention=%d days; database=%s" %
              (args.state, history.ports or "ALL", args.days, history.path.resolve()), flush=True)
        while not stopped.is_set():
            started = time.monotonic()
            row = sample(ss_path, args.state, history.ports)
            history.save(time.time(), row)
            if args.once:
                print(json.dumps(row), flush=True)
                return 0 if row["count"] is not None else 1
            # No rapid catch-up samples after a long suspension or slow collection.
            stopped.wait(max(0, INTERVAL - (time.monotonic() - started)))
    finally:
        if server:
            server.shutdown()
            server.server_close()
        lock.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print("tcp-monitor: %s" % exc, file=sys.stderr)
        sys.exit(1)
