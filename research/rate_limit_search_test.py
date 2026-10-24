"""Finds which request rate gets banned by Cloudflare.

Search requests (POST /publicaciones) are sent with shorter and shorter pauses,
then the same is done for detail requests (GET /publicaciones/{id}).
Steps go one after another without breaks, like in a long scraping run.
The first ban (429 / 403 / challenge page) ends the phase and we wait it out.

Run: uv run python -m research.rate_limit_search_test  (about 1.5 h, a ban adds ~30 min)
Nothing else should call the API while it runs.

Result on 2026-10-24 (all steps passed, no ban):
    search  10 s - 91 requests, 8 s - 114, 6 s - 151 (356 in 45 min)
    details 6 s - 121, 4 s - 181, 2 s - 361
Earlier runs got banned on search with 4 s pauses (after ~60 requests) and with ~20 requests in 10 s,
so the search limit is somewhere between 10 and 15 requests per minute. The ban lasts 30 min.
"""
import time
from collections.abc import Callable
from datetime import datetime

from curl_cffi.requests import Response
from curl_cffi.requests.exceptions import RequestException

from research.common import API_URL, new_session, save_results, search_body

SEARCH_PAUSES = (10, 8, 6)
DETAIL_PAUSES = (6, 4, 2)
SEARCH_STEP_MINUTES = 15
DETAIL_STEP_MINUTES = 12

session = new_session()
log: list[dict] = []
bans: list[dict] = []


def say(message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {message}")


def is_blocked(response: Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return response.status_code in (403, 429) or not content_type.startswith("application/json")


def search(number: int) -> Response:
    # Cycle through the first 50 pages so the requests are not all the same.
    return session.post(f"{API_URL}/publicaciones", json=search_body(page=number % 50 + 1, limit=10), timeout=60)


def make_detail_request(ids: list[str]) -> Callable[[int], Response]:
    return lambda number: session.get(f"{API_URL}/publicaciones/{ids[number % len(ids)]}", timeout=60)


def run_step(phase: str, pause: int, minutes: int, send: Callable[[int], Response]) -> dict | None:
    """Sends requests every `pause` seconds for `minutes`. Returns the ban info if we got banned."""
    say(f"{phase}: pause {pause} s for {minutes} min")
    started = time.time()
    number = 0
    while time.time() - started < minutes * 60:
        time.sleep(max(0.0, started + number * pause - time.time()))
        sent_at = time.time()
        number += 1
        try:
            response = send(number)
        except RequestException as exc:
            log.append({"phase": phase, "pause": pause, "time": sent_at, "status": type(exc).__name__})
            continue
        log.append({"phase": phase, "pause": pause, "time": sent_at, "status": response.status_code,
                    "seconds": round(time.time() - sent_at, 2)})

        if is_blocked(response):
            ban = {"phase": phase, "pause": pause, "request": number, "after_seconds": round(sent_at - started),
                   "status": response.status_code, "retry_after": response.headers.get("retry-after")}
            say(f"{phase}: BANNED on request {number} after {ban['after_seconds']} s, {ban}")
            bans.append(ban)
            return ban

    say(f"{phase}: pause {pause} s ok, {number} requests")
    return None


def wait_out_ban(ban: dict) -> None:
    retry_after = ban["retry_after"]
    seconds = int(retry_after) + 60 if retry_after and retry_after.isdigit() else 31 * 60
    say(f"waiting {seconds} s for the ban to end")
    time.sleep(seconds)
    while is_blocked(search(0)):
        say("still banned, waiting 5 more minutes")
        time.sleep(300)


def run_phase(phase: str, pauses: tuple[int, ...], minutes: int, send: Callable[[int], Response]) -> bool:
    for pause in pauses:
        if ban := run_step(phase, pause, minutes, send):
            wait_out_ban(ban)
            return True
    return False


def collect_text_ids() -> list[str]:
    """Text publications have small details, so the test measures requests and not traffic."""
    ids = []
    for page in (1, 2):
        body = search_body(page=page, tipoPublicacion="2002")
        response = session.post(f"{API_URL}/publicaciones", json=body, timeout=60)
        ids += [row["id"] for row in response.json()["datos"]["filas"]]
        time.sleep(12)
    return ids


def main() -> None:
    say("waiting 3 min so earlier requests do not count")
    time.sleep(180)
    detail_ids = collect_text_ids()

    if not run_phase("search", SEARCH_PAUSES, SEARCH_STEP_MINUTES, search):
        say("12 min break between phases")
        time.sleep(720)
    run_phase("details", DETAIL_PAUSES, DETAIL_STEP_MINUTES, make_detail_request(detail_ids))

    path = save_results("rate_limit_search_test", {"bans": bans, "requests": log})
    say(f"done, saved to {path}")


if __name__ == "__main__":
    main()
