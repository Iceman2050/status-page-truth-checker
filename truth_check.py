#!/usr/bin/env python3
"""
status-page-truth-checker
=========================
Vendors claim 99.99% uptime on their status pages. This tool measures it
yourself: it runs real HTTP probes against a vendor URL on a schedule,
computes measured uptime / error rate / latency p95, and compares the result
against the vendor's claimed SLA.

It can also fetch the vendor's own status API (statuspage.io-style JSON) or
incident RSS/Atom feed, so the verdict cites the vendor's self-report
side-by-side with your measurements.

Stdlib only. No accounts, no API keys.

Usage:
    python truth_check.py --url https://example.com --claimed-sla 99.99
    python truth_check.py --url https://example.com --probes 60 --interval 5 \\
        --status-api https://status.example.com/api/v2/summary.json \\
        --feed https://status.example.com/history.atom
    python truth_check.py --demo        # 30-second offline demo (local server)

Honest note, printed with every run: a short probe window CANNOT prove an SLA
violation. SLAs are measured over a month. This is a spot check -- but a high
error rate while the status page says "All Systems Operational" is worth
asking the vendor about.
"""

import argparse
import datetime
import json
import math
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

CAVEAT = (
    "Caveat: a short probe window CANNOT prove an SLA violation -- SLAs are "
    "measured over a month, and uptime varies by region, route, and luck. "
    "This is a spot check, not an audit."
)

DEFAULT_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def probe(url, timeout=DEFAULT_TIMEOUT):
    """Run one real HTTP probe. Returns a dict with ok/status/latency_ms/error."""
    req = urllib.request.Request(url, headers={"User-Agent": "status-page-truth-checker/1.0"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            # Drain a small amount so latency reflects a completed response.
            resp.read(65536)
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:  # timeout, DNS failure, refused connection, ...
        return {"ok": False, "status": None,
                "latency_ms": (time.monotonic() - start) * 1000.0,
                "error": "%s: %s" % (type(e).__name__, e)}
    latency_ms = (time.monotonic() - start) * 1000.0
    # The server answered: it is UP. 5xx means the service is failing.
    return {"ok": status is not None and status < 500,
            "status": status, "latency_ms": latency_ms, "error": None}


def run_probes(url, count, interval, timeout=DEFAULT_TIMEOUT):
    """Run `count` probes spaced `interval` seconds apart. Returns (results, elapsed_s)."""
    results = []
    start = time.monotonic()
    for i in range(count):
        results.append(probe(url, timeout))
        if i < count - 1:
            time.sleep(interval)
    return results, time.monotonic() - start


def percentile(sorted_vals, pct):
    """Nearest-rank percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    k = max(1, math.ceil(pct / 100.0 * len(sorted_vals)))
    return sorted_vals[min(k, len(sorted_vals)) - 1]


def compute_stats(results):
    total = len(results)
    ok = sum(1 for r in results if r["ok"])
    lats = sorted(r["latency_ms"] for r in results)
    return {
        "total": total,
        "ok": ok,
        "failed": total - ok,
        "uptime_pct": (100.0 * ok / total) if total else 0.0,
        "error_rate_pct": (100.0 * (total - ok) / total) if total else 0.0,
        "p50_ms": percentile(lats, 50),
        "p95_ms": percentile(lats, 95),
        "max_ms": lats[-1] if lats else 0.0,
    }


# ---------------------------------------------------------------------------
# Vendor self-report: status API + incident feed
# ---------------------------------------------------------------------------

def fetch_url(url, timeout=DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "status-page-truth-checker/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_status_api(api_url, timeout=DEFAULT_TIMEOUT):
    """Fetch a statuspage.io-style summary.json. Returns dict or None on failure."""
    try:
        data = json.loads(fetch_url(api_url, timeout))
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    status = data.get("status") or {}
    return {
        "indicator": status.get("indicator", "?"),
        "description": status.get("description", "?"),
        "page": (data.get("page") or {}).get("name", "?"),
    }


def _parse_date(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_feed(feed_url, days=90, timeout=DEFAULT_TIMEOUT, now=None):
    """Parse an RSS/Atom incident feed. Returns list of (title, date) in window."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=days)
    try:
        root = ET.fromstring(fetch_url(feed_url, timeout))
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    incidents = []
    # Atom
    for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "?").strip()
        date_s = (entry.findtext("{http://www.w3.org/2005/Atom}published")
                  or entry.findtext("{http://www.w3.org/2005/Atom}updated") or "")
        dt = _parse_date(date_s.strip())
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        if dt and dt >= cutoff:
            incidents.append((title, dt.date().isoformat()))
    # RSS
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "?").strip()
        date_s = (item.findtext("pubDate") or "").strip()
        dt = None
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                dt = datetime.datetime.strptime(date_s, fmt)
                break
            except Exception:
                continue
        if dt and dt >= cutoff:
            incidents.append((title, dt.date().isoformat()))
    if isinstance(incidents, dict):  # error path already returned
        return incidents
    incidents.sort(key=lambda t: t[1], reverse=True)
    return incidents


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def verdict(measured_pct, claimed_pct):
    """Compare measured uptime against the claimed SLA."""
    gap = claimed_pct - measured_pct
    if gap <= 0:
        return ("CONSISTENT",
                "measured %.2f%% meets the claimed %.2f%% (within this probe window)"
                % (measured_pct, claimed_pct))
    return ("MISLEADING",
            "claimed %.2f%% vs measured %.2f%% -- %.2f points lower"
            % (claimed_pct, measured_pct, gap))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def render_text(target, claimed_sla, stats, elapsed_s, interval, timeout,
                status_api, feed_incidents):
    L = []
    L.append("=" * 64)
    L.append(" STATUS-PAGE TRUTH CHECK")
    L.append("=" * 64)
    L.append("Target:          %s" % target)
    L.append("Probes:          %d in %.1fs (interval %.2fs, timeout %.0fs)"
             % (stats["total"], elapsed_s, interval, timeout))
    L.append("Measured uptime: %.2f%% (%d/%d ok)"
             % (stats["uptime_pct"], stats["ok"], stats["total"]))
    L.append("Error rate:      %.2f%% (%d/%d failed)"
             % (stats["error_rate_pct"], stats["failed"], stats["total"]))
    L.append("Latency:         p50 %.1fms | p95 %.1fms | max %.1fms"
             % (stats["p50_ms"], stats["p95_ms"], stats["max_ms"]))
    L.append("")
    L.append("Vendor self-report:")
    if status_api is None:
        L.append("  Status API:    (not checked -- pass --status-api)")
    elif "error" in status_api:
        L.append("  Status API:    unreachable (%s)" % status_api["error"])
    else:
        L.append('  Status API:    "%s" (indicator: %s)'
                 % (status_api["description"], status_api["indicator"]))
    if feed_incidents is None:
        L.append("  Incident feed: (not checked -- pass --feed)")
    elif isinstance(feed_incidents, dict):
        L.append("  Incident feed: unreachable (%s)" % feed_incidents["error"])
    else:
        L.append("  Incident feed: %d incident(s) in the last 90 days"
                 % len(feed_incidents))
        for title, day in feed_incidents[:5]:
            L.append("    - %s (%s)" % (title, day))
    L.append("")
    word, detail = verdict(stats["uptime_pct"], claimed_sla)
    L.append("Claimed SLA: %.2f%%   Measured: %.2f%%" % (claimed_sla, stats["uptime_pct"]))
    L.append("VERDICT: %s -- %s" % (word, detail))
    L.append("")
    L.append(CAVEAT)
    return "\n".join(L) + "\n"


def render_json(target, claimed_sla, stats, elapsed_s, interval, timeout,
                status_api, feed_incidents):
    word, detail = verdict(stats["uptime_pct"], claimed_sla)
    return json.dumps({
        "target": target,
        "claimed_sla_pct": claimed_sla,
        "probes": stats["total"],
        "elapsed_s": round(elapsed_s, 2),
        "interval_s": interval,
        "measured_uptime_pct": round(stats["uptime_pct"], 2),
        "error_rate_pct": round(stats["error_rate_pct"], 2),
        "latency_ms": {"p50": round(stats["p50_ms"], 1),
                       "p95": round(stats["p95_ms"], 1),
                       "max": round(stats["max_ms"], 1)},
        "vendor_status_api": status_api,
        "vendor_incidents_90d": feed_incidents,
        "verdict": word,
        "verdict_detail": detail,
        "caveat": CAVEAT,
    }, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Offline demo server (also used by the test suite)
# ---------------------------------------------------------------------------

class DemoHandler(BaseHTTPRequestHandler):
    """A vendor that lies: status page says 'all operational' while every 5th
    request fails with a 500. Deterministic, so tests and demos are stable."""
    counter = 0

    def log_message(self, *a):  # keep demo output clean
        pass

    def _bump(self):
        DemoHandler.counter += 1
        return DemoHandler.counter

    def do_GET(self):
        n = self._bump()
        if self.path == "/api/summary":
            body = json.dumps({
                "page": {"name": "Acme Cloud Status"},
                "status": {"indicator": "none",
                           "description": "All Systems Operational"},
            }).encode()
            self._send(200, body, "application/json")
        elif self.path == "/history.atom":
            now = datetime.datetime.now(datetime.timezone.utc)
            d1 = (now - datetime.timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
            d2 = (now - datetime.timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
            body = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<feed xmlns="http://www.w3.org/2005/Atom">\n'
                "<title>Acme Cloud Status History</title>\n"
                "<entry><title>Database failover</title>"
                "<published>%s</published></entry>\n"
                "<entry><title>Elevated API errors</title>"
                "<published>%s</published></entry>\n"
                "</feed>\n" % (d1, d2)
            ).encode()
            self._send(200, body, "application/atom+xml")
        else:
            time.sleep(0.002)
            if n % 5 == 0:
                self._send(500, b"internal error", "text/plain")
            else:
                self._send(200, b"ok", "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_demo_server():
    """Start the demo server on an ephemeral port. Returns (server, base_url)."""
    DemoHandler.counter = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/" % port


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        description="Measure a vendor's uptime yourself; compare against their claimed SLA.")
    ap.add_argument("--url", help="Vendor URL to probe (e.g. https://example.com).")
    ap.add_argument("--claimed-sla", type=float, default=99.99,
                    help="Vendor's claimed uptime %%, e.g. 99.99 (default: 99.99).")
    ap.add_argument("--probes", type=int, default=20, help="Number of probes (default: 20).")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="Seconds between probes (default: 1.0).")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="Per-probe timeout in seconds (default: 10).")
    ap.add_argument("--status-api", help="Vendor status API URL (statuspage.io summary.json).")
    ap.add_argument("--feed", help="Vendor incident RSS/Atom feed URL.")
    ap.add_argument("--demo", action="store_true",
                    help="Run the offline demo against a local test server.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return ap


def run_check(target, claimed_sla, probes, interval, timeout,
              status_api_url, feed_url, as_json=False):
    results, elapsed = run_probes(target, probes, interval, timeout)
    stats = compute_stats(results)
    status_api = fetch_status_api(status_api_url, timeout) if status_api_url else None
    feed_incidents = fetch_feed(feed_url, timeout=timeout) if feed_url else None
    if as_json:
        return render_json(target, claimed_sla, stats, elapsed, interval,
                           timeout, status_api, feed_incidents)
    return render_text(target, claimed_sla, stats, elapsed, interval,
                       timeout, status_api, feed_incidents)


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    if not (0 < args.claimed_sla <= 100):
        ap.error("--claimed-sla must be between 0 and 100")
    if args.probes < 1:
        ap.error("--probes must be >= 1")
    if args.interval < 0:
        ap.error("--interval must be >= 0")

    if args.demo:
        server, base = start_demo_server()
        try:
            out = run_check(base, args.claimed_sla, args.probes, args.interval,
                            args.timeout, base + "api/summary", base + "history.atom",
                            as_json=args.json)
        finally:
            server.shutdown()
        sys.stdout.write(out)
        return 0

    if not args.url:
        ap.error("provide --url, or use --demo for the offline demo")

    sys.stdout.write(run_check(args.url, args.claimed_sla, args.probes, args.interval,
                               args.timeout, args.status_api, args.feed,
                               as_json=args.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
