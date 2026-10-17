#!/usr/bin/env python3
"""Tests for truth_check.py. All network I/O goes to a local test server --
no external requests, no mocks of the core logic."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import truth_check as tc


class ServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = tc.start_demo_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


class TestProbe(ServerMixin, unittest.TestCase):
    def test_probe_200_is_ok(self):
        r = tc.probe(self.base, timeout=5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], 200)
        self.assertIsNone(r["error"])
        self.assertGreaterEqual(r["latency_ms"], 0)

    def test_probe_500_is_failure(self):
        # Every 5th request fails; hammer until we see one (deterministic server).
        seen_500 = False
        for _ in range(10):
            r = tc.probe(self.base, timeout=5)
            if r["status"] == 500:
                seen_500 = True
                self.assertFalse(r["ok"])
                break
        self.assertTrue(seen_500)

    def test_probe_connection_refused(self):
        r = tc.probe("http://127.0.0.1:1/", timeout=2)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["status"])
        self.assertIsNotNone(r["error"])


class TestStats(unittest.TestCase):
    def _results(self, oks, fails):
        rs = [{"ok": True, "status": 200, "latency_ms": 10.0 * (i + 1), "error": None}
              for i in range(oks)]
        rs += [{"ok": False, "status": 500, "latency_ms": 5.0, "error": None}
               for _ in range(fails)]
        return rs

    def test_uptime_and_error_rate(self):
        s = tc.compute_stats(self._results(16, 4))
        self.assertEqual(s["total"], 20)
        self.assertAlmostEqual(s["uptime_pct"], 80.0)
        self.assertAlmostEqual(s["error_rate_pct"], 20.0)

    def test_percentiles(self):
        # 16 ok latencies 10..160 plus 4 failure latencies of 5.0.
        # Sorted: [5,5,5,5,10,20,...,160] (20 values).
        # p50 -> rank ceil(0.5*20)=10 -> 60.0; p95 -> rank ceil(0.95*20)=19 -> 150.0
        s = tc.compute_stats(self._results(16, 4))
        self.assertAlmostEqual(s["p50_ms"], 60.0)
        self.assertAlmostEqual(s["p95_ms"], 150.0)

    def test_percentile_nearest_rank(self):
        self.assertEqual(tc.percentile([1, 2, 3, 4], 50), 2)
        self.assertEqual(tc.percentile([1, 2, 3, 4], 100), 4)
        self.assertEqual(tc.percentile([], 95), 0.0)

    def test_empty_results(self):
        s = tc.compute_stats([])
        self.assertEqual(s["uptime_pct"], 0.0)
        self.assertEqual(s["p95_ms"], 0.0)


class TestVerdict(unittest.TestCase):
    def test_misleading(self):
        word, detail = tc.verdict(80.0, 99.99)
        self.assertEqual(word, "MISLEADING")
        self.assertIn("99.99", detail)
        self.assertIn("80.00", detail)

    def test_consistent(self):
        word, _ = tc.verdict(99.99, 99.99)
        self.assertEqual(word, "CONSISTENT")

    def test_consistent_when_measured_higher(self):
        word, _ = tc.verdict(100.0, 99.9)
        self.assertEqual(word, "CONSISTENT")


class TestVendorSelfReport(ServerMixin, unittest.TestCase):
    def test_status_api_parsed(self):
        info = tc.fetch_status_api(self.base + "api/summary", timeout=5)
        self.assertEqual(info["description"], "All Systems Operational")
        self.assertEqual(info["indicator"], "none")

    def test_status_api_unreachable(self):
        info = tc.fetch_status_api("http://127.0.0.1:1/api", timeout=2)
        self.assertIn("error", info)

    def test_feed_incidents_counted(self):
        inc = tc.fetch_feed(self.base + "history.atom", timeout=5)
        self.assertIsInstance(inc, list)
        self.assertEqual(len(inc), 2)
        titles = [t for t, _ in inc]
        self.assertIn("Database failover", titles)

    def test_feed_unreachable(self):
        inc = tc.fetch_feed("http://127.0.0.1:1/feed", timeout=2)
        self.assertIn("error", inc)


class TestDemoEndToEnd(unittest.TestCase):
    def test_demo_prints_misleading_and_caveat(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tc.main(["--demo", "--probes", "10", "--interval", "0.01",
                          "--claimed-sla", "99.99"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # 10 probes, every 5th fails -> exactly 80.00% measured.
        self.assertIn("80.00%", out)
        self.assertIn("MISLEADING", out)
        self.assertIn("claimed 99.99% vs measured 80.00%", out)
        self.assertIn("CANNOT prove an SLA violation", out)
        self.assertIn("All Systems Operational", out)
        self.assertIn("2 incident(s)", out)

    def test_demo_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tc.main(["--demo", "--probes", "10", "--interval", "0.01", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["verdict"], "MISLEADING")
        self.assertAlmostEqual(data["measured_uptime_pct"], 80.0)
        self.assertIn("caveat", data)


class TestCliValidation(unittest.TestCase):
    def test_bad_sla_rejected(self):
        with self.assertRaises(SystemExit):
            tc.main(["--url", "http://127.0.0.1:1", "--claimed-sla", "101"])

    def test_missing_url_rejected(self):
        with self.assertRaises(SystemExit):
            tc.main(["--probes", "2"])

    def test_zero_probes_rejected(self):
        with self.assertRaises(SystemExit):
            tc.main(["--url", "http://127.0.0.1:1", "--probes", "0"])


if __name__ == "__main__":
    unittest.main()
