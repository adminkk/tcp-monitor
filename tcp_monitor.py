#!/usr/bin/env python3
"""Sample Linux TCP sockets every 60 seconds; retain a bounded JSON-lines file."""

import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

INTERVAL = 60
DATA_NAME = "tcp_connections.txt"
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


class History:
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

    def save(self, now, row=None):
        # A clock correction must not retain records outside the configured window.
        self.rows = deque((r for r in self.rows
                           if now - self.days * 86400 <= r["timestamp"] <= now),
                          maxlen=self.limit)
        if row is not None:
            self.rows.append(row)
        temporary = self.path.with_name(".tcp_connections.tmp")
        try:
            with temporary.open(mode="w", encoding="utf-8", newline="\n") as target:
                for item in self.rows:
                    target.write(json.dumps(item, separators=(",", ":")) + "\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()


def make_server(bind, port, data_path, html_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_file(False)

        def do_HEAD(self):
            self.send_file(True)

        def send_file(self, head):
            route = urlsplit(self.path).path
            if route in ("/", "/index.html"):
                path, mime = html_path, "text/html; charset=utf-8"
            elif route == "/" + DATA_NAME:
                path, mime = data_path, "text/plain; charset=utf-8"
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
        history = History(args.output_dir / DATA_NAME, args.days, args.state, sorted(set(args.port)))
        history.save(time.time())
        if args.http_port and not args.once:
            server = make_server(args.bind, args.http_port, history.path, html_path)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print("Dashboard: http://%s:%d/" % (args.bind, args.http_port), flush=True)
        print("TCP state=%s local_ports=%s retention=%d days; file=%s" %
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
    except (OSError, RuntimeError) as exc:
        print("tcp-monitor: %s" % exc, file=sys.stderr)
        sys.exit(1)
