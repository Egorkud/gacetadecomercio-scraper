import time
from typing import Any, Self

from curl_cffi import requests
from curl_cffi.requests import Response
from curl_cffi.requests.exceptions import RequestException

from src.config import Settings
from src.logger import LoggerMixin

RETRYABLE_STATUSES = frozenset({403, 429, 500, 502, 503, 504})


class ScraperError(Exception):
    pass


class HttpError(ScraperError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


class RequestFailedError(ScraperError):
    pass


class BaseScraper(LoggerMixin):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session(impersonate=settings.impersonate, headers=settings.headers)
        self._last_request_at = 0.0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

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

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        """Throttled request, retried on network errors, Cloudflare blocks and 5xx."""
        url = self.build_url(url)
        kwargs.setdefault("timeout", self.settings.timeout)
        retries = self.settings.max_retries
        reason = ""

        for attempt in range(1, retries + 1):
            self._throttle()
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
            raise ScraperError(f"Invalid JSON from {response.url}: {response.text[:300]}") from exc

    def build_url(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        return f"{self.settings.api_base_url.rstrip('/')}/{url.lstrip('/')}"

    def _throttle(self) -> None:
        wait = self.settings.min_request_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

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
        return "text/html" in content_type and "Just a moment" in response.text[:2000]
