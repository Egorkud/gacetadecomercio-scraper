"""Measures how fast publication details can be downloaded and whether it gets banned.

For PDF types and for text types it tries:
- one request after another, like the scraper does now (with and without a pause);
- async requests with N at the same time.
Each step runs for a few minutes or until a request limit. A ban stops the section,
more than 20% errors stops raising N.

Run: uv run python -m research.download_concurrency_test  (about 40 min, a ban adds ~30 min)
Downloads a few GB, PDFs are not saved.

Result on 2026-10-25, no ban at all:
    PDF   sequential 27/min, x5 30/min, x10 40/min, x20 70/min, x40 102/min (p95 latency 75 s)
    text  sequential 227/min, x10 2 334/min, x80 12 477/min
Details are not rate limited. PDFs are slow on the server side (~2 s each),
more parallel requests help, but latency grows fast after x20.
"""
import asyncio
import json
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from research.common import API_URL, SETTINGS, new_session, save_results, search_body

PDF_TYPES = ("1009", "1001", "1003", "1005", "1010")
TEXT_TYPES = ("2002", "2001", "2004")

PDF_PLAN = {"pauses": (2, 0), "workers": (3, 5, 10, 20, 40), "minutes": 3, "max_requests": 1000}
TEXT_PLAN = {"pauses": (0,), "workers": (5, 10, 20, 40, 80), "minutes": 2, "max_requests": 3000}


def say(message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {message}")


@dataclass
class Step:
    name: str
    started: float = field(default_factory=time.time)
    statuses: Counter = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)
    ok: int = 0
    errors: int = 0
    without_pdf: int = 0
    bytes: int = 0
    blocked: bool = False
    retry_after: str | None = None

    @property
    def requests(self) -> int:
        return self.ok + self.errors + int(self.blocked)

    def add(self, status: int, headers: dict, body: bytes, latency: float) -> None:
        self.statuses[status] += 1
        self.bytes += len(body)
        content_type = headers.get("content-type", "")
        if status in (403, 429) or (status == 200 and not content_type.startswith("application/json")):
            self.blocked = True
            self.retry_after = headers.get("retry-after")
            return
        try:
            data = json.loads(body) if status == 200 else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or not data.get("finalizado"):
            self.errors += 1
            return
        self.ok += 1
        self.latencies.append(latency)
        if not ((data.get("datos") or {}).get("documento") or "").startswith("JVBER"):  # base64 of "%PDF"
            self.without_pdf += 1

    def add_failure(self, exc: Exception) -> None:
        self.statuses[type(exc).__name__] += 1
        self.errors += 1

    def summary(self) -> dict:
        seconds = time.time() - self.started
        latencies = sorted(self.latencies) or [0.0]
        return {
            "step": self.name,
            "requests": self.requests,
            "ok": self.ok,
            "errors": self.errors,
            "blocked": self.blocked,
            "retry_after": self.retry_after,
            "per_minute": round(self.requests / seconds * 60, 1),
            "mb_per_second": round(self.bytes / 1e6 / seconds, 2),
            "latency_median": round(statistics.median(latencies), 2),
            "latency_p95": round(latencies[int(len(latencies) * 0.95)], 2),
            "latency_max": round(latencies[-1], 2),
            "without_pdf": self.without_pdf,
            "statuses": {str(status): count for status, count in self.statuses.items()},
        }


def collect_ids(types: tuple[str, ...]) -> list[str]:
    """Two pages of 100 per type, 8 s apart to stay under the search limit."""
    session = new_session()
    ids = []
    for code in types:
        for page in (1, 2):
            body = search_body(page=page, tipoPublicacion=code)
            response = session.post(f"{API_URL}/publicaciones", json=body, timeout=60)
            ids += [row["id"] for row in response.json()["datos"]["filas"]]
            time.sleep(8)
    return list(dict.fromkeys(ids))


def run_sequential(name: str, ids: list[str], pause: float, minutes: float, max_requests: int) -> Step:
    session = new_session()
    step = Step(name)
    for number in range(max_requests):
        if time.time() - step.started > minutes * 60 or step.blocked:
            break
        time.sleep(max(0.0, step.started + number * pause - time.time()))
        sent_at = time.time()
        try:
            response = session.get(f"{API_URL}/publicaciones/{ids[number % len(ids)]}", timeout=120)
        except RequestException as exc:
            step.add_failure(exc)
            continue
        step.add(response.status_code, response.headers, response.content, time.time() - sent_at)
    return step


async def run_concurrent(name: str, ids: list[str], workers: int, minutes: float, max_requests: int) -> Step:
    step = Step(name)
    numbers = iter(range(max_requests))

    async with AsyncSession(impersonate=SETTINGS.impersonate, headers=SETTINGS.headers, max_clients=workers) as session:
        async def worker() -> None:
            for number in numbers:
                if time.time() - step.started > minutes * 60 or step.blocked:
                    return
                sent_at = time.time()
                try:
                    response = await session.get(f"{API_URL}/publicaciones/{ids[number % len(ids)]}", timeout=120)
                except RequestException as exc:
                    step.add_failure(exc)
                    continue
                step.add(response.status_code, response.headers, response.content, time.time() - sent_at)

        await asyncio.gather(*(worker() for _ in range(workers)))
    return step


def finish(step: Step, results: list[dict]) -> dict:
    summary = step.summary()
    results.append(summary)
    say(
        f"{summary['step']:<18} {summary['requests']:>5} req  {summary['per_minute']:>8}/min  "
        f"{summary['mb_per_second']:>5} MB/s  latency {summary['latency_median']}/{summary['latency_p95']}/"
        f"{summary['latency_max']} s  errors {summary['errors']}  {'BANNED' if summary['blocked'] else ''}"
    )
    return summary


def wait_out_ban(summary: dict) -> None:
    retry_after = summary["retry_after"]
    seconds = int(retry_after) + 60 if retry_after and retry_after.isdigit() else 31 * 60
    say(f"waiting {seconds} s for the ban to end")
    time.sleep(seconds)


def run_section(label: str, ids: list[str], plan: dict, results: list[dict]) -> None:
    for pause in plan["pauses"]:
        step = run_sequential(f"{label} pause {pause}s", ids, pause, plan["minutes"], plan["max_requests"])
        if (summary := finish(step, results))["blocked"]:
            wait_out_ban(summary)
            return

    for workers in plan["workers"]:
        name = f"{label} async x{workers}"
        step = asyncio.run(run_concurrent(name, ids, workers, plan["minutes"], plan["max_requests"]))
        summary = finish(step, results)
        if summary["blocked"]:
            wait_out_ban(summary)
            return
        if summary["errors"] > summary["requests"] * 0.2:
            say(f"{label}: more than 20% errors, not going higher")
            return
        time.sleep(20)


def main() -> None:
    say("collecting ids")
    pdf_ids = collect_ids(PDF_TYPES)
    text_ids = collect_ids(TEXT_TYPES)
    say(f"{len(pdf_ids)} PDF ids, {len(text_ids)} text ids")

    results: list[dict] = []
    run_section("PDF", pdf_ids, PDF_PLAN, results)
    time.sleep(60)
    run_section("text", text_ids, TEXT_PLAN, results)
    say(f"done, saved to {save_results('download_concurrency_test', results)}")


if __name__ == "__main__":
    main()
