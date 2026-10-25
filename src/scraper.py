import threading
import time
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from curl_cffi import requests
from curl_cffi.requests import Response
from curl_cffi.requests.exceptions import RequestException

from src.config import Settings
from src.helpers.utils import slugify, timestamp
from src.logger import LoggerMixin

RETRYABLE_STATUSES = frozenset({403, 429, 500, 502, 503, 504})


class ScraperError(Exception):
    pass


class HttpError(ScraperError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {body}")
        self.status = status
        self.url = url
        self.body = body


class RequestFailedError(ScraperError):
    pass


class RateLimiter:
    """Keeps at least `interval` seconds between request starts, shared by all threads."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_at)
            self._next_at = start_at + self.interval
        time.sleep(start_at - now)


class BaseScraper(LoggerMixin):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.limiter = RateLimiter(settings.search_interval)
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def session(self) -> requests.Session:
        """Session of the current thread; a curl handle must not be shared between threads."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session(impersonate=self.settings.impersonate, headers=self.settings.headers)
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Response:
        return self.request("GET", url, params=params, **kwargs)

    def post(
            self,
            url: str,
            json: Any = None,
            data: Any = None,
            **kwargs: Any,
    ) -> Response:
        return self.request("POST", url, json=json, data=data, **kwargs)

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.parse_json(self.get(url, params=params, **kwargs))

    def post_json(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        return self.parse_json(self.post(url, json=json, **kwargs))

    def request(self, method: str, url: str, limiter: RateLimiter | None = None, **kwargs: Any) -> Response:
        """Rate limited request, retried on network errors, Cloudflare blocks and 5xx.

        Uses `self.limiter` unless another limiter is given.
        """
        limiter = limiter or self.limiter
        url = self.build_url(url)
        kwargs.setdefault("timeout", self.settings.timeout)
        retries = self.settings.max_retries
        reason = ""

        for attempt in range(1, retries + 1):
            limiter.wait()
            try:
                response = self.session.request(method, url, **kwargs)
            except RequestException as exc:
                reason, delay = f"{type(exc).__name__}: {exc}", self._backoff(attempt)
            else:
                if response.ok and not self._is_challenge(response):
                    self.logger.debug("%s %s -> %s", method, url, response.status_code)
                    return response
                if not response.ok and response.status_code not in RETRYABLE_STATUSES:
                    raise HttpError(response.status_code, url, response.text)
                reason = "Cloudflare challenge" if response.ok else f"HTTP {response.status_code}"
                delay = self._retry_delay(response, attempt)

            if attempt < retries:
                self.logger.warning(
                    "%s %s failed (%s), retry %d/%d in %.0fs", method, url, reason, attempt, retries - 1, delay,
                )
                time.sleep(delay)

        raise RequestFailedError(f"{method} {url} failed after {retries} attempts: {reason}")

    def parse_json(self, response: Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            path = self._save_response(response)
            raise ScraperError(
                f"Invalid JSON from {response.url} (HTTP {response.status_code}, "
                f"{response.headers.get('content-type')}, {len(response.content)} bytes), body saved to {path}"
            ) from exc

    def build_url(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        return f"{self.settings.api_base_url.rstrip('/')}/{url.lstrip('/')}"

    def _save_response(self, response: Response) -> Path:
        """Keep a broken response in full; it can be megabytes of base64, too big for a log line."""
        name = slugify(urlsplit(response.url).path)
        path = self.settings.logs_dir / "responses" / f"{timestamp(precise=True)}_{name}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
        return path

    def _backoff(self, attempt: int) -> float:
        return min(self.settings.backoff_base * 2 ** (attempt - 1), self.settings.backoff_max)

    def _retry_delay(self, response: Response, attempt: int) -> float:
        """Follow Retry-After when given; a ban longer than max_retry_after is not waited out."""
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            seconds = float(retry_after)
            if seconds > self.settings.max_retry_after:
                raise RequestFailedError(
                    f"Retry-After {seconds:.0f}s exceeds limit for {response.url}"
                )
            return seconds + 1
        return self._backoff(attempt)

    @staticmethod
    def _is_challenge(response: Response) -> bool:
        if response.headers.get("cf-mitigated") == "challenge":
            return True
        content_type = response.headers.get("content-type", "")
        return "text/html" in content_type and "Just a moment" in response.text
