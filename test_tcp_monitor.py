"""Isolated tests: no real TCP scan, no production services."""
import json
from pathlib import Path
import subprocess
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tcp_monitor import DB_NAME, DATA_NAME, History, make_server, sample


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / DATA_NAME
        self.db_path = self.path.with_name(DB_NAME)
        self.now = 1800000000

    def row(self, age=0, count=12):
        return {"timestamp": self.now - age, "state": "established", "ports": [40002], "count": count}

    def write(self, rows):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_restart_prunes_old_and_future_rows_and_keeps_boundary(self):
        self.write([self.row(86401), self.row(86400), self.row(60), self.row(-60)])
        history = History(self.db_path, 1, "established", [40002], self.now)
        history.save(self.now, self.row())
        rows = history.rows
        self.assertEqual([r["timestamp"] for r in rows],
                         [self.now - 86400, self.now - 60, self.now])
        self.assertFalse(self.path.with_name(".tcp_connections.tmp").exists())
        history.save(self.now + 61)
        self.assertEqual(len(history.rows), 2)

    def test_repeated_restarts_cannot_grow_file_without_bound(self):
        self.write([self.row()] * 2000)
        history = History(self.db_path, 1, "established", [40002], self.now)
        history.save(self.now, self.row())
        self.assertEqual(len(history.rows), 1441)
        self.assertEqual(len(History(self.db_path, 1, "established", [], self.now).rows), 1441)

    def test_state_change_rejected_and_original_file_untouched(self):
        self.write([self.row()])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "another TCP state"):
            History(self.db_path, 7, "all", [40002], self.now)
        self.assertEqual(self.path.read_bytes(), before)

    def test_port_changes_preserve_each_records_scope_and_breakdown(self):
        single = dict(self.row(180), port_counts={"40002": 12})
        multi = dict(self.row(120), ports=[40002, 40000], port_counts={"40000": 5, "40002": 7})
        removed = dict(self.row(60), ports=[40000], port_counts={"40000": 12})
        whole_host = dict(self.row(), ports=[], port_counts={})
        self.write([single, multi, removed, whole_host])
        history = History(self.db_path, 7, "established", [11000, 40002], self.now)
        new_row = dict(self.row(), ports=[11000, 40002], port_counts={"11000": 2, "40002": 10})
        history.save(self.now, new_row)
        reloaded = History(self.db_path, 7, "established", [40000], self.now)
        expected = [single, dict(multi, ports=[40000, 40002]), removed, whole_host, new_row]
        self.assertEqual(list(reloaded.rows), expected)
        self.assertEqual(reloaded.ports, [40000])

    def test_retention_applies_to_all_port_profiles(self):
        old = dict(self.row(86401), ports=[11000], port_counts={"11000": 12})
        recent = dict(self.row(60), ports=[40000], port_counts={"40000": 12})
        self.write([old, recent, self.row()])
        history = History(self.db_path, 1, "established", [], self.now)
        history.save(self.now)
        self.assertEqual([r["ports"] for r in history.rows], [[40000], [40002]])
        history.save(self.now + 86401)
        self.assertEqual(history.rows, [])

    def test_invalid_port_lists_are_not_preserved(self):
        self.write([dict(self.row(), ports=ports) for ports in
                    (None, [0], [65536], ["40002"], [40002, 40002], [True])]
                   + [self.row()])
        history = History(self.db_path, 7, "established", [40000], self.now)
        self.assertEqual(list(history.rows), [self.row()])

    def test_malformed_history_is_skipped_and_failure_is_retained(self):
        self.write([self.row(count=None)])
        with self.path.open("a") as f:
            f.write('broken\n{"timestamp":NaN,"count":1}\n[]\n')
        history = History(self.db_path, 1, "established", [40002], self.now)
        self.assertEqual(len(history.rows), 1)
        self.assertIsNone(history.rows[0]["count"])

    def test_ss_port_filter_and_count(self):
        result = subprocess.CompletedProcess([], 0,
            "0 0 127.0.0.1:40002 127.0.0.1:50000\n\n"
            "ESTAB 0 0 [::ffff:10.0.0.1]:40000 [::ffff:10.0.0.2]:50001\n"
            "ESTAB 0 0 [fe80::1%eth0]:40002 [fe80::2%eth0]:50002\n", "")
        with patch("tcp_monitor.subprocess.run", return_value=result) as run:
            row = sample("/usr/bin/ss", "established", [40000, 40002], self.now)
        self.assertEqual(row["count"], 3)
        self.assertEqual(row["port_counts"], {"40000": 1, "40002": 2})
        self.assertEqual(run.call_args.args[0], ["/usr/bin/ss", "-H", "-t", "-n", "-a",
                                               "state", "established",
                                               "( sport = :40000 or sport = :40002 )"])

    def test_zero_connections_and_parse_failure_for_port_breakdown(self):
        with patch("tcp_monitor.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            row = sample("ss", "established", [40000, 40002], self.now)
        self.assertEqual(row["port_counts"], {"40000": 0, "40002": 0})
        self.assertEqual(row["count"], 0)
        for output in ("unrecognized output", "0 0 127.0.0.1:9999 127.0.0.1:50000"):
            with self.subTest(output=output), patch("tcp_monitor.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, output, "")):
                row = sample("ss", "established", [40000, 40002], self.now)
                self.assertIsNone(row["count"])
                self.assertEqual(row["port_counts"], {"40000": None, "40002": None})
                self.assertEqual(row["error"], "ss_output_unrecognized")

    def test_port_breakdown_and_legacy_history_survive_restart(self):
        legacy = self.row(120)
        modern = dict(self.row(60), port_counts={"40002": 12})
        failed = dict(self.row(count=None), port_counts={"40002": None})
        self.write([legacy, modern, failed])
        history = History(self.db_path, 1, "established", [40002], self.now)
        history.save(self.now)
        again = History(self.db_path, 1, "established", [40002], self.now)
        self.assertEqual(list(again.rows), [legacy, modern, dict(failed, error="collection_failed")])

    def test_invalid_breakdowns_are_skipped(self):
        rows = [dict(self.row(), port_counts={"40002": -1}),
                dict(self.row(), port_counts={"40002": 11}),
                dict(self.row(), port_counts={"40000": 12}),
                dict(self.row(count=None), port_counts={"40002": 0}),
                dict(self.row(), port_counts={"40002": 12})]
        self.write(rows)
        history = History(self.db_path, 1, "established", [40002], self.now)
        self.assertEqual(len(history.rows), 1)
        self.assertEqual(history.rows[0]["port_counts"], {"40002": 12})

    def test_migration_is_once_only_and_leaves_text_unchanged(self):
        self.write([self.row(60)])
        original = self.path.read_bytes()
        history = History(self.db_path, 1, "established", [], self.now)
        history.save(self.now, self.row())
        self.assertEqual(self.path.read_bytes(), original)
        again = History(self.db_path, 1, "established", [], self.now)
        self.assertEqual(again.rows, [self.row(60), self.row()])
        with again.connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failed_migration_rolls_back_and_can_be_retried(self):
        self.write([self.row(60), dict(self.row(), state="all")])
        with self.assertRaises(RuntimeError):
            History(self.db_path, 1, "established", [], self.now)
        self.write([self.row(60), self.row()])
        history = History(self.db_path, 1, "established", [], self.now)
        self.assertEqual(history.rows, [self.row(60), self.row()])

    def test_failed_save_rolls_back_pruning_and_keeps_count_correct(self):
        self.write([self.row(86400)])
        history = History(self.db_path, 1, "established", [], self.now)
        with patch.object(history, "_insert", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                history.save(self.now + 60, self.row())
        self.assertEqual(history.rows, [self.row(86400)])
        history.save(self.now + 60, self.row())
        self.assertEqual(history.rows, [self.row()])
        self.assertEqual(history._count, 1)

    def test_wal_reader_snapshot_does_not_block_sample_write(self):
        history = History(self.db_path, 1, "established", [], self.now)
        history.save(self.now, self.row())
        with history.connect() as reader, reader:
            reader.execute("BEGIN")
            self.assertEqual(reader.execute("SELECT count(*) FROM samples").fetchone()[0], 1)
            history.save(self.now + 60, dict(self.row(), timestamp=self.now + 60))
            self.assertEqual(reader.execute("SELECT count(*) FROM samples").fetchone()[0], 1)
        self.assertEqual(len(history.rows), 2)

    def test_query_range_keeps_global_latest_and_port_options(self):
        self.write([dict(self.row(120), ports=[11000]), self.row(60), self.row()])
        history = History(self.db_path, 1, "established", [], self.now)
        result = history.query(self.now - 60, self.now - 60)
        self.assertEqual(result["records"], [self.row(60)])
        self.assertEqual(result["latest"], self.row())
        self.assertEqual(result["ports"], [11000, 40002])
        history.save(self.now + 86401)
        self.assertEqual(history.query()["ports"], [])

    def test_database_state_change_is_rejected(self):
        history = History(self.db_path, 7, "established", [], self.now)
        history.save(self.now, self.row())
        with self.assertRaisesRegex(RuntimeError, "another TCP state"):
            History(self.db_path, 7, "all", [], self.now)
        self.assertEqual(history.rows, [self.row()])

    def test_ss_failure_is_missing_not_zero(self):
        cases = [(subprocess.TimeoutExpired("ss", 20), "ss_timeout"),
                 (OSError("missing"), "ss_unavailable")]
        for exc, message in cases:
            with self.subTest(message=message), patch("tcp_monitor.subprocess.run", side_effect=exc):
                row = sample("ss", "all", [], self.now)
                self.assertIsNone(row["count"])
                self.assertEqual(row["error"], message)
        with patch("tcp_monitor.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "bad")):
            self.assertEqual(sample("ss", "all", [], self.now)["error"], "ss_exit_1")
        with patch("tcp_monitor.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(sample("ss", "all", [], self.now)["count"], 0)

    def test_http_serves_only_dashboard_and_complete_text_without_cache(self):
        self.write([self.row()])
        history = History(self.db_path, 7, "established", [], self.now)
        server = make_server("127.0.0.1", 0, history, Path(__file__).with_name("index.html"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            address = "http://127.0.0.1:%d" % server.server_port
            with urlopen(address + "/" + DATA_NAME + "?refresh=1", timeout=5) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.loads(response.read())["count"], 12)
            with urlopen(address, timeout=5) as response:
                self.assertIn("TCP 连接趋势", response.read().decode("utf-8"))
            with urlopen(address + "/api/history?start=%s&end=%s" % (self.now, self.now), timeout=5) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.loads(response.read())["records"], [self.row()])
            with urlopen(address + "/api/history?end=1", timeout=5) as response:
                result = json.loads(response.read())
                self.assertEqual(result["records"], [])
                self.assertEqual(result["latest"], self.row())
            for route in ("/api/history", "/" + DATA_NAME):
                with urlopen(Request(address + route, method="HEAD"), timeout=5) as response:
                    self.assertEqual(response.read(), b"")
            for query in ("start=NaN", "end=Infinity", "start=2&end=1", "start=", "start=1&start=2", "sql=DELETE"):
                with self.subTest(query=query), self.assertRaises(HTTPError) as invalid:
                    urlopen(address + "/api/history?" + query, timeout=5)
                self.assertEqual(invalid.exception.code, 400)
            with self.assertRaises(HTTPError) as database:
                urlopen(address + "/" + DB_NAME, timeout=5)
            self.assertEqual(database.exception.code, 404)
            with self.assertRaises(HTTPError) as error:
                urlopen(address + "/../tcp_monitor.py", timeout=5)
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
