import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

# ============ CONFIG - PASTE TOKEN HERE ============
TOKEN = ""  # <-- PASTE YOUR TOKEN HERE
BASE_URL = "https://api.runkai.ai"
ENDPOINTS = [
    ("GET", "/v1/actions/types"),
    ("GET", "/v1/actions/search/text?limit=10&offset=0"),
    ("GET", "/v1/action_results/?limit=100&offset=0"),
    ("GET", "/v1/app-creds/types"),
]
CONCURRENCY = 50
# ===================================================


@dataclass
class Result:
    task_id: int
    endpoint: str
    status: int
    size: int
    latency_ms: float
    error: str | None


async def hit(client: httpx.AsyncClient, method: str, endpoint: str, task_id: int) -> Result:
    start = time.perf_counter()
    try:
        r = await client.request(method, f"{BASE_URL}{endpoint}")
        latency = (time.perf_counter() - start) * 1000
        return Result(task_id, endpoint, r.status_code, len(r.content), latency, None)
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return Result(task_id, endpoint, 0, 0, latency, str(e)[:100])


def generate_report(results: list[Result], total_time: float) -> str:
    lines = []
    lines.append(f"Load Test Report - {datetime.now().isoformat()}")
    lines.append(f"Base URL: {BASE_URL} | Concurrency: {CONCURRENCY}")
    lines.append("")

    successes = [r for r in results if r.status == 200]
    failures = [r for r in results if r.status != 200]

    lines.append("=" * 85)
    lines.append(f"{'ENDPOINT':<40} | OK | FAIL | MIN ms | AVG ms | MAX ms")
    lines.append("=" * 85)

    for _, ep in ENDPOINTS:
        ep_results = [r for r in results if r.endpoint == ep]
        ok = sum(1 for r in ep_results if r.status == 200)
        fail = len(ep_results) - ok
        latencies = [r.latency_ms for r in ep_results]
        min_ms = min(latencies) if latencies else 0
        avg = sum(latencies) / len(latencies) if latencies else 0
        max_ms = max(latencies) if latencies else 0
        lines.append(f"{ep:<40} | {ok:2d} | {fail:4d} | {min_ms:6.0f} | {avg:6.0f} | {max_ms:6.0f}")

    lines.append("=" * 85)
    lines.append(f"Total: {len(results)} | Success: {len(successes)} | Failed: {len(failures)}")
    avg_latency = sum(r.latency_ms for r in results) / len(results) if results else 0
    lines.append(f"Total time: {total_time:.2f}s | Avg latency: {avg_latency:.0f}ms")

    if failures:
        lines.append("")
        lines.append("--- Failed Requests ---")
        for r in failures:
            lines.append(f"  Task {r.task_id:2d} | {r.status} | {r.latency_ms:5.0f}ms | {r.endpoint[:35]} | {r.error}")

    return "\n".join(lines)


async def main():
    assert TOKEN, "Paste your token in TOKEN variable at the top"

    headers = {"Authorization": f"Bearer {TOKEN}"}
    timeout = httpx.Timeout(connect=10, read=120, write=10, pool=10)

    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        tasks = [
            asyncio.create_task(hit(client, method, ep, i))
            for i in range(CONCURRENCY)
            for method, ep in ENDPOINTS
        ]
        start = time.perf_counter()
        results: list[Result] = await asyncio.gather(*tasks)
        total_time = time.perf_counter() - start

    report = generate_report(results, total_time)
    print(report)

    # Save to results folder
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    filename = results_dir / f"load_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    filename.write_text(report)
    print(f"\n📄 Saved: {filename}")


if __name__ == "__main__":
    asyncio.run(main())
