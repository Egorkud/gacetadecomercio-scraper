import math
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.config import Settings
from src.csv_service import CsvService
from src.file_service import FileService, FileServiceError
from src.models import Category, CsvRecord, Publication, PublicationDetail, parse_date
from src.scraper import BaseScraper, ScraperError

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
class SearchResult:
    total: int
    rows: list[dict[str, Any]]


@dataclass(slots=True)
class RunStats:
    pdf_saved: int = 0
    pdf_generated: int = 0
    skipped: int = 0
    failed: int = 0
    per_type: Counter[str] = field(default_factory=Counter)


class GacetaScraper(BaseScraper):
    def __init__(self, settings: Settings, csv_service: CsvService, file_service: FileService) -> None:
        super().__init__(settings)
        self.csv = csv_service
        self.files = file_service
        self.stats = RunStats()
        self._seen_ids: set[str] = set()

    def run(self) -> RunStats:
        date_to = self.settings.date_to or date.today()
        date_from = self.settings.date_from
        types = self.select_types()
        self.logger.info("Collecting %d types from %s to %s", len(types), date_from, date_to)
        self.check_catalog()

        # One request per type over the whole range: small types finish here, the rest go by windows.
        # Rows come newest first, so the first row tells where the type's publications start.
        large: dict[str, tuple[Category, date]] = {}
        for category in types:
            result = self.search(category.code, date_from, date_to)
            self.logger.info("[%s] %s: %d publications", category.code, category.name, result.total)
            if result.total > self.settings.page_size:
                newest = parse_date(result.rows[0]["fechaPublicacionFormato"]) or date_to
                large[category.code] = (category, newest)
            else:
                self._consume(category, iter(result.rows))

        for window_from, window_to in self.iter_windows(date_from, date_to):
            if not large:
                break
            active = {code: item for code, item in large.items() if item[1] >= window_from}
            if not active:
                continue
            self.logger.info("Window %s..%s, %d types left", window_from, window_to, len(active))
            for code, (category, _) in active.items():
                if not self._consume(category, self.iter_range(code, window_from, window_to)):
                    del large[code]

        self.log_summary()
        return self.stats

    def select_types(self) -> list[Category]:
        source = ALL_PUBLICATION_TYPES if self.settings.all_types else TASK_PUBLICATION_TYPES
        codes = self.settings.categories or tuple(source)
        unknown = [code for code in codes if code not in ALL_PUBLICATION_TYPES]
        if unknown:
            raise ValueError(f"Unknown publication types: {', '.join(unknown)}")
        return [Category(code, ALL_PUBLICATION_TYPES[code]) for code in codes]

    def check_catalog(self) -> None:
        items = self._api_post("publicaciones/catalogos/lista", {"codigo": ""})
        live = {str(item["codigoParametrica"]) for item in items}
        if added := live - TASK_PUBLICATION_TYPES.keys():
            self.logger.warning("Catalog has new types not in TASK_PUBLICATION_TYPES: %s", sorted(added))
        if removed := TASK_PUBLICATION_TYPES.keys() - live:
            self.logger.warning("Types missing from the live catalog: %s", sorted(removed))

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
        return SearchResult(total=int(data["total"]), rows=data["filas"])

    def get_detail(self, publication_id: str, category: Category) -> PublicationDetail:
        data = self._api_get(f"publicaciones/{publication_id}")
        return PublicationDetail.from_api(data, category)

    def iter_windows(self, date_from: date, date_to: date) -> Iterator[tuple[date, date]]:
        window_to = date_to
        while window_to >= date_from:
            window_from = max(date_from, window_to - timedelta(days=self.settings.window_days - 1))
            yield window_from, window_to
            window_to = window_from - timedelta(days=1)

    def iter_range(self, type_code: str, date_from: date, date_to: date) -> Iterator[dict[str, Any]]:
        """Yield rows newest first; ranges over one page are split, so pagination is avoided."""
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
        for part_from, part_to in self._split(date_from, date_to, parts):
            yield from self.iter_range(type_code, part_from, part_to)

    def _paginate_day(self, type_code: str, day: date, first: SearchResult) -> Iterator[dict[str, Any]]:
        # Rows sharing a timestamp are ordered randomly between requests, so pages may overlap or miss rows.
        rows = {row["id"]: row for row in first.rows}
        pages = math.ceil(first.total / self.settings.page_size)
        for page in range(2, pages + 1):
            rows.update((row["id"], row) for row in self.search(type_code, day, day, page).rows)
        if len(rows) != first.total:
            self.logger.warning("[%s] %s: got %d of %d rows via pagination", type_code, day, len(rows), first.total)
        yield from rows.values()

    @staticmethod
    def _split(date_from: date, date_to: date, parts: int) -> list[tuple[date, date]]:
        days = (date_to - date_from).days + 1
        size = math.ceil(days / parts)
        ranges = []
        part_to = date_to
        while part_to >= date_from:
            part_from = max(date_from, part_to - timedelta(days=size - 1))
            ranges.append((part_from, part_to))
            part_to = part_from - timedelta(days=1)
        return ranges

    def _consume(self, category: Category, rows: Iterator[dict[str, Any]]) -> bool:
        """Process rows until the per-type limit; return False once the limit is reached."""
        limit = self.settings.limit_per_type
        for row in rows:
            if limit is not None and self.stats.per_type[category.code] >= limit:
                return False
            if row["id"] in self._seen_ids:
                continue
            self._seen_ids.add(row["id"])
            self.stats.per_type[category.code] += 1
            self.process(Publication.from_api(row, category))
        return limit is None or self.stats.per_type[category.code] < limit

    def process(self, publication: Publication) -> None:
        category = publication.category
        file_name = self.files.build_filename(publication.company_name, publication.matricula, publication.code)
        path = self.files.build_path(file_name, f"{category.code} - {category.name}")
        if self.files.exists(path):
            self.stats.skipped += 1
            self.logger.debug("Skip existing %s", path.name)
            return

        try:
            if category.code in TEXT_PUBLICATION_TYPES:
                self._save_text(path, publication, publication.summary)
            else:
                self._save_from_detail(path, publication)
        except (ScraperError, FileServiceError) as exc:
            self.stats.failed += 1
            self.logger.error("Failed %s (id %s): %s", publication.code, publication.id, exc)
            return

        relative_path = path.relative_to(self.files.files_dir).as_posix()
        self.csv.write_row(CsvRecord.from_publication(publication, relative_path, datetime.now()).to_row())

    def _save_from_detail(self, path: Path, publication: Publication) -> None:
        detail = self.get_detail(publication.id, publication.category)
        if detail.has_document:
            self.files.save_base64_pdf(path, detail.document_b64)
            self.stats.pdf_saved += 1
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
        self.stats.pdf_generated += 1
        self.logger.info("Generated PDF %s", path.name)

    def log_summary(self) -> None:
        s = self.stats
        self.logger.info(
            "Done: %d PDFs saved, %d generated, %d skipped, %d failed",
            s.pdf_saved, s.pdf_generated, s.skipped, s.failed,
        )
        for code, count in sorted(s.per_type.items()):
            self.logger.info("  [%s] %d publications", code, count)

    def _api_post(self, url: str, payload: dict[str, Any]) -> Any:
        return self._unwrap(self.post_json(url, json=payload), url)

    def _api_get(self, url: str) -> Any:
        return self._unwrap(self.get_json(url), url)

    @staticmethod
    def _unwrap(body: dict[str, Any], url: str) -> Any:
        if not body.get("finalizado"):
            errors = (body.get("datos") or {}).get("errores") or []
            raise ScraperError(f"API error for {url}: {body.get('mensaje')} {errors}".strip())
        return body["datos"]
