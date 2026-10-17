# status-page-truth-checker

**Every vendor's status page says 99.99% uptime. Nobody checks.** This tool checks: it runs real HTTP probes against a vendor URL on a schedule, computes measured uptime, error rate, and latency p95, pulls the vendor's own status API / incident feed for their side of the story, and prints the comparison:

> claimed 99.99% vs measured 80.00% — MISLEADING

Stdlib only. No accounts, no API keys, no daemon.

## Quickstart (30 seconds)

```bash
git clone https://github.com/Iceman2050/status-page-truth-checker.git
cd status-page-truth-checker
python3 truth_check.py --demo --probes 20 --interval 0.1
```

The `--demo` flag spins up a local test server that *lies like a vendor*: its status API reports "All Systems Operational" while every 5th request fails with a 500. Fully offline, ~2 seconds.

Against a real vendor (be polite — 20 probes is a spot check, not a load test):

```bash
python3 truth_check.py --url https://example.com --claimed-sla 99.99 \
  --probes 60 --interval 5 \
  --status-api https://status.example.com/api/v2/summary.json \
  --feed https://status.example.com/history.atom
```

## Example output

Real output from `python3 truth_check.py --demo --probes 20 --interval 0.1` (port varies per run; this was one real run):

```
{{EXAMPLE_OUTPUT}}
```

## What it does NOT do

- **A short probe window cannot prove an SLA violation.** SLAs are measured over a full month, across regions and routes. This tool is a spot check — it gives you evidence to ask hard questions, not a legal finding. The caveat prints with every run.
- **It does not verify *why* something failed.** A 500 could be the vendor, your network, or a proxy in between. Correlate with the vendor's incident feed before concluding anything.
- **It is not a monitoring system.** For ongoing SLA tracking, run it on a schedule (cron) and log the `--json` output; the tool itself keeps no history.
- **Status API / feed parsing is best-effort.** It understands statuspage.io-style `summary.json` and standard RSS/Atom feeds; exotic vendor formats may parse partially.

## Development

```bash
python3 -m unittest discover -s tests -v   # 19 tests, stdlib only, local test server
```

## License

MIT — see [LICENSE](LICENSE).

{{BUILT_BY}}
