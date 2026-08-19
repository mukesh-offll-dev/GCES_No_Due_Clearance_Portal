#!/usr/bin/env python3
"""
Dependency-free load tester for the GCES No Due portal.

Measures raw request-serving capacity (throughput + latency percentiles) by
firing N concurrent GET requests at a URL for a fixed duration. Use it to find
the maximum stable concurrency your server sustains before latency degrades.

Examples
--------
    # 200 concurrent clients hammering the login page for 30s
    python scripts/loadtest.py http://localhost:8000/ -c 200 -d 30

    # Ramp: try increasing concurrency until p95 latency climbs
    python scripts/loadtest.py http://localhost:8000/ -c 400 -d 20

Notes
-----
* This hits an UNAUTHENTICATED page by default (the login page), which still
  exercises Django, middleware, sessions and WhiteNoise. For authenticated
  flows (dashboards, status API, approvals) use the Locust file in
  loadtest/locustfile.py instead.
* Run it from a DIFFERENT machine than the server for realistic numbers.
"""
import sys
import time
import argparse
import threading
import statistics
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def _worker(url, stop_at, latencies, errors, timeout):
    while time.monotonic() < stop_at:
        start = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                resp.read()
                if resp.status >= 500:
                    errors.append(resp.status)
                else:
                    latencies.append((time.monotonic() - start) * 1000)
        except Exception:
            errors.append("exc")


def main():
    ap = argparse.ArgumentParser(description="Simple concurrent load tester")
    ap.add_argument("url")
    ap.add_argument("-c", "--concurrency", type=int, default=100)
    ap.add_argument("-d", "--duration", type=int, default=30, help="seconds")
    ap.add_argument("-t", "--timeout", type=float, default=30.0)
    args = ap.parse_args()

    latencies, errors = [], []
    stop_at = time.monotonic() + args.duration
    print(f"Load testing {args.url}  concurrency={args.concurrency}  "
          f"duration={args.duration}s")

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for _ in range(args.concurrency):
            pool.submit(_worker, args.url, stop_at, latencies, errors, args.timeout)
    elapsed = time.monotonic() - started

    total = len(latencies) + len(errors)
    ok = len(latencies)
    print("\n──────────── RESULTS ────────────")
    print(f"Requests:        {total}")
    print(f"Successful:      {ok}")
    print(f"Errors/5xx:      {len(errors)}")
    print(f"Throughput:      {ok / elapsed:.1f} req/s")
    if latencies:
        latencies.sort()
        def pct(p):
            return latencies[min(len(latencies) - 1, int(len(latencies) * p))]
        print(f"Latency  avg:    {statistics.mean(latencies):.1f} ms")
        print(f"Latency  p50:    {pct(0.50):.1f} ms")
        print(f"Latency  p95:    {pct(0.95):.1f} ms")
        print(f"Latency  p99:    {pct(0.99):.1f} ms")
        print(f"Latency  max:    {max(latencies):.1f} ms")
    if total and len(errors) / total > 0.01:
        print("\n⚠️  Error rate > 1% — you have likely exceeded stable capacity.")
        sys.exit(1)


if __name__ == "__main__":
    main()
