"""Helpers shared by the research scripts.

Run the scripts from the project root as modules, for example:
    uv run python -m research.api_completeness_check
Results are saved to research/results/.
"""
import json
import math
import sys
from pathlib import Path
from typing import Any

from curl_cffi import requests
from curl_cffi.requests import Response

from src.config import Settings
from src.scraper import BaseScraper

SETTINGS = Settings()
API_URL = SETTINGS.api_base_url
RESULTS_DIR = Path(__file__).parent / "results"

# The site sends every filter on each search, unused ones as "".
EMPTY_FILTERS = {
    "fecha": "",
    "fechaInicial": "",
    "fechaFinal": "",
    "categoria": "",
    "tipoPublicacion": "",
    "razonSocial": "",
    "matricula": "",
    "resumen": "",
}

# Windows console uses cp1251 and fails on Spanish characters.
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def search_body(page: int = 1, limit: int = 100, **filters: str) -> dict[str, Any]:
    return {**EMPTY_FILTERS, **filters, "limite": limit, "pagina": page}


def date_filter(first_day: str, last_day: str | None = None) -> dict[str, str]:
    """Custom date range, both days included."""
    return {"fecha": "5", "fechaInicial": first_day, "fechaFinal": last_day or first_day}


def new_session() -> requests.Session:
    """Bare session without pauses and retries, used by the rate limit tests."""
    return requests.Session(impersonate=SETTINGS.impersonate, headers=SETTINGS.headers)


def save_results(name: str, data: Any) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


class ResearchApi(BaseScraper):
    """BaseScraper with shortcuts for the public endpoints."""

    def __init__(self, settings: Settings = SETTINGS) -> None:
        super().__init__(settings)

    def catalog(self) -> list[dict[str, Any]]:
        return self.post_json("publicaciones/catalogos/lista", json={"codigo": ""})["datos"]

    def search(self, page: int = 1, limit: int = 100, **filters: str) -> dict[str, Any]:
        return self.post_json("publicaciones", json=search_body(page, limit, **filters))["datos"]

    def search_all(self, limit: int = 100, **filters: str) -> list[dict[str, Any]]:
        first = self.search(1, limit, **filters)
        rows = list(first["filas"])
        for page in range(2, math.ceil(first["total"] / limit) + 1):
            rows += self.search(page, limit, **filters)["filas"]
        return rows

    def total(self, **filters: str) -> int:
        return self.search(limit=10, **filters)["total"]

    def detail(self, publication_id: str) -> dict[str, Any]:
        return self.get_json(f"publicaciones/{publication_id}")["datos"]

    def raw(self, method: str, url: str, **kwargs: Any) -> Response:
        """Single request without retries that never raises on HTTP errors."""
        self._throttle()
        return self.session.request(method, self.build_url(url), timeout=self.settings.timeout, **kwargs)
