import argparse
import json
import os
import statistics
import time

import requests
from dotenv import load_dotenv


def quote_once(base_url, token, symbol):
    started_at = time.perf_counter()
    response = requests.get(
        f"{base_url}/markets/quotes",
        params={"symbols": symbol},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=10,
    )
    latency_ms = int((time.perf_counter() - started_at) * 1000)
    quote_signature = None
    if response.ok:
        try:
            data = response.json()
            quote = data.get("quotes", {}).get("quote")
            if isinstance(quote, list):
                quote = quote[0] if quote else {}
            if isinstance(quote, dict):
                quote_signature = json.dumps({
                    "last": quote.get("last"),
                    "bid": quote.get("bid"),
                    "ask": quote.get("ask"),
                    "volume": quote.get("volume"),
                    "trade_date": quote.get("trade_date"),
                    "biddate": quote.get("biddate"),
                    "askdate": quote.get("askdate"),
                }, sort_keys=True)
        except Exception:
            quote_signature = None
    return response.status_code, latency_ms, response.text[:250], quote_signature


def benchmark_interval(base_url, token, symbol, interval_ms, duration_seconds):
    latencies = []
    failures = 0
    rate_limited = 0
    responses = {}
    quote_changes = 0
    duplicate_quotes = 0
    last_quote_signature = None
    started_at = time.perf_counter()
    next_request_at = started_at

    while time.perf_counter() - started_at < duration_seconds:
        sleep_seconds = next_request_at - time.perf_counter()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        status, latency_ms, text, quote_signature = quote_once(base_url, token, symbol)
        latencies.append(latency_ms)
        responses[status] = responses.get(status, 0) + 1

        if quote_signature is not None:
            if last_quote_signature is None:
                quote_changes += 1
            elif quote_signature == last_quote_signature:
                duplicate_quotes += 1
            else:
                quote_changes += 1
            last_quote_signature = quote_signature

        if status == 429:
            rate_limited += 1
        if status >= 400:
            failures += 1
            print(f"FAIL interval={interval_ms}ms status={status} latency={latency_ms}ms body={text}")

        next_request_at += interval_ms / 1000

    requests_per_minute = int(len(latencies) * (60 / duration_seconds)) if duration_seconds else 0
    freshness_percent = round((quote_changes / len(latencies)) * 100, 1) if latencies else 0

    return {
        "interval_ms": interval_ms,
        "requests": len(latencies),
        "requests_per_minute": requests_per_minute,
        "avg_latency_ms": int(statistics.mean(latencies)) if latencies else None,
        "median_latency_ms": int(statistics.median(latencies)) if latencies else None,
        "slowest_latency_ms": max(latencies) if latencies else None,
        "failures": failures,
        "rate_limited": rate_limited,
        "quote_changes": quote_changes,
        "duplicate_quotes": duplicate_quotes,
        "freshness_percent": freshness_percent,
        "responses": responses,
    }


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Benchmark Tradier quote polling intervals.")
    parser.add_argument("--symbol", default="SPY", help="Symbol or option symbol to quote.")
    parser.add_argument("--duration", type=int, default=60, help="Seconds per interval.")
    parser.add_argument("--intervals", default="1000,750,500,250", help="Comma-separated intervals in ms.")
    parser.add_argument("--min-freshness", type=float, default=90.0, help="Minimum quote freshness percent required for stable recommendation.")
    args = parser.parse_args()

    token = os.getenv("TRADIER_TOKEN", "")
    base_url = os.getenv("TRADIER_BASE_URL", "https://sandbox.tradier.com/v1")

    if not token:
        raise SystemExit("TRADIER_TOKEN is missing. Set it in .env before running this benchmark.")

    intervals = [int(value.strip()) for value in args.intervals.split(",") if value.strip()]

    print("Tradier quote polling benchmark")
    print("base_url:", base_url)
    print("symbol:", args.symbol)
    print("duration_per_interval_seconds:", args.duration)
    print("minimum_freshness_percent:", args.min_freshness)
    print()

    results = []
    for interval_ms in intervals:
        print(f"Testing {interval_ms} ms...")
        result = benchmark_interval(base_url, token, args.symbol, interval_ms, args.duration)
        results.append(result)
        print(result)
        print("requests_per_minute:", result["requests_per_minute"])
        print("quote_changes:", result["quote_changes"])
        print("duplicate_quotes:", result["duplicate_quotes"])
        print("freshness_percent:", result["freshness_percent"])
        print()

    stable = [
        result for result in results
        if (
            result["failures"] == 0
            and result["rate_limited"] == 0
            and result["freshness_percent"] >= args.min_freshness
        )
    ]
    if stable:
        fastest = min(stable, key=lambda result: result["interval_ms"])
        print("FASTEST_STABLE_INTERVAL_MS:", fastest["interval_ms"])
    else:
        print("FASTEST_STABLE_INTERVAL_MS: none")


if __name__ == "__main__":
    main()
