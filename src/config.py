from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Without Origin/Referer the API answers with a Cloudflare challenge page.
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Origin": "https://gacetadecomercio.gob.bo",
    "Sec-Fetch-Storage-Access": "none",
    "Connection": "keep-alive",
    "Referer": "https://gacetadecomercio.gob.bo/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "DNT": "1",
    "Sec-GPC": "1",
    "Priority": "u=0",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}


@dataclass(frozen=True, slots=True)
class Settings:
    site_url: str = "https://gacetadecomercio.gob.bo"
    api_base_url: str = "https://servicios.seprec.gob.bo/api"

    # TLS fingerprint matches the Firefox User-Agent in DEFAULT_HEADERS.
    impersonate: str = "firefox147"
    timeout: float = 60.0

    page_size: int = 100  # API accepts 10..100
    min_request_interval: float = 12.0
    max_retries: int = 5
    backoff_base: float = 5.0
    backoff_max: float = 300.0
    max_retry_after: float = 1800.0

    # The oldest publication in the API is from 2017-06-14.
    date_from: date = date(2017, 1, 1)
    date_to: date | None = None
    window_days: int = 30
    split_target: int = 70  # expected rows per request when a range is split

    limit_per_type: int | None = None
    categories: tuple[str, ...] = ()
    all_types: bool = False

    output_dir: Path = PROJECT_ROOT / "output"
    logs_dir: Path = PROJECT_ROOT / "logs"
    log_level: str = "INFO"

    @property
    def files_dir(self) -> Path:
        return self.output_dir / "files"

    @property
    def headers(self) -> dict[str, str]:
        return dict(DEFAULT_HEADERS)
