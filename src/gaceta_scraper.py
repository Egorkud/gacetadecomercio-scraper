import json
import math
import threading
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.config import Settings
from src.csv_service import CsvService
from src.download_pool import DownloadPool
from src.failed_registry import FailedRegistry
from src.file_service import FileService, FileServiceError
from src.models import Category, CsvRecord, MetadataRecord, Publication, PublicationDetail, SearchResult, parse_date
from src.scraper import BaseScraper, HttpError, RateLimiter, ScraperError

# Types offered by the site's "Publicación" dropdown (POST publicaciones/catalogos/lista).
TASK_PUBLICATION_TYPES: dict[str, str] = {
    # 1 - Instrumentos
    "1001": "Aumento de Capital Social",
    "1002": "Constitución de Sociedad Comercial S.A., Sociedad en Comandita por Acciones Constituidas por Acto Único o por Suscripción Pública de Acciones",
    "1003": "Constitución de Sociedad Comercial S.R.L., Sociedad Colectiva o Sociedad en Comandita Simple",
    "1004": "Constitución de Sociedad Constituida en el Extranjero",
    "1005": "Constitución de Sociedad de Economía Mixta",
    "1007": "Escisión",
    "1008": "Fusión de Sociedades",
    "1009": "Modificación, Aclaración y/o Complementación de la Constitución de Sociedad Comercial",
    "1010": "Reducción de Capital Social",
    "1012": "Transformación",
    "1013": "Cierre de Sucursal o Representación Permanente de Sociedad Extranjera",
    "1019": "Disolución y Liquidación de Sociedad Comercial",
    "1020": "Transferencia de Cuotas de Capital",
    # 2 - Convocatorias y avisos
    "2000": "Cancelación Anticipada de Títulos",
    "2001": "Convocatoria a Asamblea/Junta General",
    "2002": "Notificación de Cambio de Dirección",
    "2003": "Proyecto de Distribución de Patrimonio",
    "2004": "Reposición de Títulos Nominativos",
    "2005": "Resultado del Sorteo de Títulos Valores",
    "2006": "Transferencia de Empresa",
    "2007": "Ofrecimiento de Acciones para Ejercicio de Derecho Preferente",
    "2008": "Edicto de auto de quiebra",
    # 3 - Memorias
    "3000": "Memoria Anual",
    # 4 - Balances
    "4000": "Publicación de Balance Anual para Emisión de Bonos",
}

# Legacy types missing from the catalog but accepted by the API; visible on the site only under "Todas".
HIDDEN_PUBLICATION_TYPES: dict[str, str] = {
    "1006": "Tipo no listado 1006",
    "1011": "Tipo no listado 1011",
    "1016": "Tipo no listado 1016",
    "1018": "Tipo no listado 1018",
}

ALL_PUBLICATION_TYPES: dict[str, str] = TASK_PUBLICATION_TYPES | HIDDEN_PUBLICATION_TYPES

# These never carry a PDF; their list `resumen` equals the detail `textoCompleto`.
TEXT_PUBLICATION_TYPES: frozenset[str] = frozenset({"2001", "2002", "2004", "2005", "2006", "2007"})


@dataclass(slots=True)
class RunStats:
    pdf_saved: int = 0
    pdf_generated: int = 0
    skipped: int = 0
    failed: int = 0
    metadata_rows: int = 0
    per_type: Counter[str] = field(default_factory=Counter)
    expected_per_type: Counter[str] = field(default_factory=Counter)


class GacetaScraper(BaseScraper):
    """Collects publications type by type, newest first.

    The API loses rows when paginating, so every request has to fit into one page:
    big types go by date windows, and windows that are still too big are split further.
    Searches run one by one in the main thread, downloads go to a pool of worker threads.
    """

    def __init__(
        self,
        settings: Settings,
        csv_service: CsvService,
        file_service: FileService,
        failed: FailedRegistry,
    ) -> None:
        super().__init__(settings)
        self.csv = csv_service
        self.files = file_service
        self.failed = failed
        self.stats = RunStats()
        self.download_limiter = RateLimiter(settings.download_interval)
        self._downloads = DownloadPool(settings.download_workers)
        self._stats_lock = threading.Lock()
        self._seen_ids: set[str] = set()
        self._failed_this_run: set[str] = set()

    def close(self) -> None:
        self._downloads.shutdown()
        super().close()

    def run(self) -> RunStats:
        date_from = self.settings.date_from
        date_to = self.settings.date_to or date.today()
        types = self.select_types()
        self.logger.info("Collecting %d types from %s to %s", len(types), date_from, date_to)
        self.check_catalog()

        large_types = self._collect_small_types(types, date_from, date_to)
        self._collect_by_windows(large_types, date_from, date_to)
        return self._finish()

    def retry_failed(self, only_ids: set[str] | None = None) -> RunStats:
        """Retry publications from failed.csv: all of them or only `only_ids`."""
        records = [record for record in self.failed.records() if only_ids is None or record.id in only_ids]
        self.logger.info("Retrying %d failed publications", len(records))
        for record in records:
            name = ALL_PUBLICATION_TYPES.get(record.type_code, f"Tipo {record.type_code}")
            publication = record.to_publication(Category(record.type_code, name))
            # failed.csv keeps no text, so even text types go through the detail.
            self._downloads.submit(self.process, publication, force_detail=True)
        self._downloads.wait()
        if only_ids is None:
            self.log_summary()
        return self.stats

    def _finish(self) -> RunStats:
        self._downloads.wait()
        if self._failed_this_run:
            # Server errors are often temporary, so this run's failures get one more try.
            self.retry_failed(set(self._failed_this_run))
        self.log_summary()
        return self.stats

    def select_types(self) -> list[Category]:
        source = ALL_PUBLICATION_TYPES if self.settings.all_types else TASK_PUBLICATION_TYPES
        codes = self.settings.categories or tuple(source)
        if unknown := [code for code in codes if code not in ALL_PUBLICATION_TYPES]:
            raise ValueError(f"Unknown publication types: {', '.join(unknown)}")
        return [Category(code, ALL_PUBLICATION_TYPES[code]) for code in codes]

    def check_catalog(self) -> None:
        """Warn if the site catalog no longer matches TASK_PUBLICATION_TYPES."""
        items = self._api_post("publicaciones/catalogos/lista", {"codigo": ""})
        live = {str(item["codigoParametrica"]) for item in items}
        if added := live - TASK_PUBLICATION_TYPES.keys():
            self.logger.warning("Catalog has new types not in TASK_PUBLICATION_TYPES: %s", sorted(added))
        if removed := TASK_PUBLICATION_TYPES.keys() - live:
            self.logger.warning("Types missing from the live catalog: %s", sorted(removed))

    def _collect_small_types(self, types: list[Category], date_from: date, date_to: date) -> dict[Category, date]:
        """Process types that fit into one page; return the others with their newest publication date."""
        large_types = {}
        for category in types:
            result = self.search(category.code, date_from, date_to)
            self.logger.info("[%s] %s: %d publications", category.code, category.name, result.total)
            self.stats.expected_per_type[category.code] = result.total
            if result.total <= self.settings.page_size:
                self._consume(category, result.rows)
            else:
                # Rows come newest first, so windows after this date are empty for the type.
                large_types[category] = parse_date(result.rows[0]["fechaPublicacionFormato"]) or date_to
        return large_types

    def _collect_by_windows(self, large_types: dict[Category, date], date_from: date, date_to: date) -> None:
        for window_from, window_to in self._date_ranges(date_from, date_to, self.settings.window_days):
            if not large_types:
                return
            active = [category for category, newest in large_types.items() if newest >= window_from]
            if not active:
                continue
            self.logger.info("Window %s..%s, %d types left", window_from, window_to, len(active))
            for category in active:
                if not self._consume(category, self._iter_range(category.code, window_from, window_to)):
                    del large_types[category]

    def _iter_range(self, type_code: str, date_from: date, date_to: date) -> Iterator[dict[str, Any]]:
        """Yield rows newest first, splitting the range until each request fits into one page."""
        result = self.search(type_code, date_from, date_to)
        if result.total <= self.settings.page_size:
            yield from result.rows
            return

        days = (date_to - date_from).days + 1
        if days == 1:
            yield from self._paginate_day(type_code, date_from, result)
            return

        parts = min(days, math.ceil(result.total / self.settings.split_target))
        self.logger.debug("[%s] %s..%s: %d rows, split into %d", type_code, date_from, date_to, result.total, parts)
        for part_from, part_to in self._date_ranges(date_from, date_to, math.ceil(days / parts)):
            yield from self._iter_range(type_code, part_from, part_to)

    def _paginate_day(self, type_code: str, day: date, first: SearchResult) -> Iterator[dict[str, Any]]:
        """Last resort for a day over one page.

        Rows sharing a timestamp come in a different order on every request, so pages overlap
        and miss rows. The day is paged again until every row is seen or the rounds run out.
        """
        rows = {row["id"]: row for row in first.rows}
        pages = math.ceil(first.total / self.settings.page_size)
        rounds = 0
        while len(rows) < first.total and rounds < self.settings.pagination_rounds:
            rounds += 1
            # The first page of round 1 is the request that found the day too big.
            for page in range(2 if rounds == 1 else 1, pages + 1):
                page_rows = self.search(type_code, day, day, page).rows
                if not page_rows:
                    break  # past the real data, whatever total the API reported
                rows.update((row["id"], row) for row in page_rows)
                if len(rows) >= first.total:
                    break

        if len(rows) != first.total:
            self.logger.warning(
                "[%s] %s: got %d of %d rows via pagination after %d rounds",
                type_code, day, len(rows), first.total, rounds,
            )
        else:
            self.logger.info("[%s] %s: %d rows via pagination, %d rounds", type_code, day, len(rows), rounds)
        yield from rows.values()

    @staticmethod
    def _date_ranges(date_from: date, date_to: date, days: int) -> Iterator[tuple[date, date]]:
        """Consecutive ranges of `days` days, newest first."""
        range_to = date_to
        while range_to >= date_from:
            range_from = max(date_from, range_to - timedelta(days=days - 1))
            yield range_from, range_to
            range_to = range_from - timedelta(days=1)

    def search(self, type_code: str, date_from: date, date_to: date, page: int = 1) -> SearchResult:
        payload = {
            "fecha": "5",
            "fechaInicial": date_from.isoformat(),
            "fechaFinal": date_to.isoformat(),
            "categoria": "",
            "tipoPublicacion": type_code,
            "razonSocial": "",
            "matricula": "",
            "resumen": "",
            "limite": self.settings.page_size,
            "pagina": page,
        }
        data = self._api_post("publicaciones", payload)
        return SearchResult.from_api(data)

    def get_detail(self, publication_id: str, category: Category) -> PublicationDetail:
        data = self._api_get(f"publicaciones/{publication_id}", limiter=self.download_limiter)
        return PublicationDetail.from_api(data, category)

    def _api_post(self, url: str, payload: dict[str, Any]) -> Any:
        return self._unwrap(self.post_json(url, json=payload), url)

    def _api_get(self, url: str, limiter: RateLimiter | None = None) -> Any:
        return self._unwrap(self.get_json(url, limiter=limiter), url)

    @staticmethod
    def _unwrap(body: dict[str, Any], url: str) -> Any:
        if not body.get("finalizado"):
            errors = (body.get("datos") or {}).get("errores") or []
            raise ScraperError(f"API error for {url}: {body.get('mensaje')} {errors}".strip())
        return body["datos"]

    def _consume(self, category: Category, rows: Iterable[dict[str, Any]]) -> bool:
        """Process new rows of a type; return False once limit_per_type is reached."""
        for row in rows:
            if self._limit_reached(category):
                return False
            if row["id"] in self._seen_ids:
                continue
            self._seen_ids.add(row["id"])
            self.stats.per_type[category.code] += 1
            publication = Publication.from_api(row, category)
            if self.settings.metadata_only:
                self.save_metadata(publication)
            else:
                self._downloads.submit(self.process, publication)
        return not self._limit_reached(category)

    def _limit_reached(self, category: Category) -> bool:
        limit = self.settings.limit_per_type
        return limit is not None and self.stats.per_type[category.code] >= limit

    def save_metadata(self, publication: Publication) -> None:
        """Write the list row to metadata.csv; file_name is where `process` would save the PDF."""
        file_name = self._relative_name(self._file_path(publication))
        self.csv.write_row(MetadataRecord.from_publication(publication, file_name, datetime.now()).to_row())
        self.stats.metadata_rows += 1

    def process(self, publication: Publication, force_detail: bool = False) -> None:
        """Save a publication as PDF and add it to the CSV; failures go to failed.csv. Runs in a worker thread."""
        path = self._file_path(publication)
        if self.files.exists(path):
            self._count("skipped")
            self.failed.remove(publication.id)
            self.logger.debug("Skip existing %s", path.name)
            return

        try:
            if publication.category.code in TEXT_PUBLICATION_TYPES and not force_detail:
                self._save_text(path, publication, publication.summary)
            else:
                self._save_from_detail(path, publication)
        except (ScraperError, FileServiceError) as exc:
            self._mark_failed(publication, exc)
            return

        self.failed.remove(publication.id)
        self._write_csv_row(publication, path)

    def _file_path(self, publication: Publication) -> Path:
        category = publication.category
        file_name = self.files.build_filename(publication.company_name, publication.matricula, publication.code)
        return self.files.build_path(file_name, f"{category.code} - {category.name}")

    def _save_from_detail(self, path: Path, publication: Publication) -> None:
        detail = self.get_detail(publication.id, publication.category)
        if detail.has_document:
            self.files.save_base64_pdf(path, detail.document_b64)
            self._count("pdf_saved")
            self.logger.info("Saved PDF %s", path.name)
            return

        if detail.previous_code:
            self.logger.warning("No document for %s (site shows 'temporalmente no disponible')", detail.code)
        if detail.full_text_html:
            self._save_text(path, detail, detail.full_text_html, is_html=True)
        else:
            self._save_text(path, detail, detail.summary)

    def _save_text(self, path: Path, publication: Publication, body: str, is_html: bool = False) -> None:
        meta = [
            f"Código de publicación: {publication.code}",
            f"Fecha de publicación: {publication.published_at or ''}",
            f"Matrícula de comercio: {publication.matricula_display}",
            f"Denominación / Razón social: {publication.company_name}",
        ]
        self.files.save_text_as_pdf(path, publication.title, body, is_html=is_html, meta=meta)
        self._count("pdf_generated")
        self.logger.info("Generated PDF %s", path.name)

    def _write_csv_row(self, publication: Publication, path: Path) -> None:
        file_name = self._relative_name(path)
        self.csv.write_row(CsvRecord.from_publication(publication, file_name, datetime.now()).to_row())

    def _relative_name(self, path: Path) -> str:
        return path.relative_to(self.files.files_dir).as_posix()

    def _mark_failed(self, publication: Publication, exc: Exception) -> None:
        reason = self._failure_reason(exc)
        with self._stats_lock:
            self.stats.failed += 1
            self._failed_this_run.add(publication.id)
        self.failed.add(publication, reason)
        self.logger.error("Failed %s (id %s): %s", publication.code, publication.id, reason)

    def _count(self, stat: str) -> None:
        """Increment a RunStats counter; called from download threads."""
        with self._stats_lock:
            setattr(self.stats, stat, getattr(self.stats, stat) + 1)

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        """Short reason for failed.csv, e.g. "HTTP 412: Error al obtener el archivo"."""
        if not isinstance(exc, HttpError):
            return str(exc)
        try:
            data = json.loads(exc.body)
        except ValueError:
            data = None
        message = data.get("mensaje") if isinstance(data, dict) else None
        return f"HTTP {exc.status}: {message or exc.body}"

    def log_summary(self) -> None:
        stats = self.stats
        if self.settings.metadata_only:
            self.logger.info("Done: %d metadata rows", stats.metadata_rows)
        else:
            self.logger.info(
                "Done: %d PDFs saved, %d generated, %d skipped, %d failed attempts",
                stats.pdf_saved, stats.pdf_generated, stats.skipped, stats.failed,
            )
        for code, count in sorted(stats.per_type.items()):
            self.logger.info("  [%s] %d publications", code, count)
        self._check_completeness()
        if len(self.failed) and not self.settings.metadata_only:
            still_failed = sum(publication_id in self.failed for publication_id in self._failed_this_run)
            self.logger.warning(
                "%d publications of this run are unavailable on the server, %d in total; see %s",
                still_failed, len(self.failed), self.failed.path,
            )

    def _check_completeness(self) -> None:
        """Compare collected publications per type with the totals the API reported at the start."""
        if self.settings.limit_per_type is not None or not self.stats.expected_per_type:
            return
        mismatches = {
            code: (self.stats.per_type[code], expected)
            for code, expected in self.stats.expected_per_type.items()
            if self.stats.per_type[code] != expected
        }
        if not mismatches:
            self.logger.info("Complete: every type matches the API total")
        for code, (collected, expected) in sorted(mismatches.items()):
            # New publications during a long run also show up here.
            self.logger.warning("[%s] collected %d of %d publications reported by the API", code, collected, expected)
