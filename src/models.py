from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from typing import Any, Self

from src.helpers.utils import format_date, parse_date


class CsvRow:
    """Fieldnames and row dict for dataclasses stored in CSV files."""

    __slots__ = ()

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Category:
    code: str
    name: str

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Self:
        return cls(code=str(item["codigoParametrica"]), name=item["nombreParametrica"])


@dataclass(slots=True)
class SearchResult:
    """Page of POST publicaciones: total matches and up to `limite` raw rows."""

    total: int
    rows: list[dict[str, Any]]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Self:
        return cls(total=int(data["total"]), rows=data["filas"])


@dataclass(slots=True)
class Publication:
    """Row of the publication list."""

    id: str
    code: str
    title: str
    summary: str
    published_at: date | None
    company_name: str
    matricula: str
    matricula_anterior: str | None = None
    category: Category | None = None

    @property
    def matricula_display(self) -> str:
        if self.matricula_anterior:
            return f"{self.matricula} ({self.matricula_anterior})"
        return self.matricula

    @classmethod
    def from_api(cls, item: dict[str, Any], category: Category | None = None) -> Self:
        company = item.get("empresa") or {}
        return cls(
            id=str(item["id"]),
            code=item.get("codigo") or "",
            title=(item.get("titulo") or "").strip(),
            summary=(item.get("resumen") or "").strip(),
            published_at=parse_date(item.get("fechaPublicacionFormato") or item.get("fechaPublicacion")),
            company_name=(company.get("razonSocial") or "").strip(),
            matricula=company.get("matricula") or "",
            matricula_anterior=company.get("matriculaAnterior"),
            category=category,
        )


@dataclass(slots=True)
class PublicationDetail(Publication):
    """Response of GET publicaciones/{id}; `documento` is a base64 PDF."""

    previous_code: str | None = None
    full_text_html: str | None = None
    archive_path: str | None = None
    document_b64: str | None = None

    @property
    def has_document(self) -> bool:
        return bool(self.document_b64)

    @classmethod
    def from_api(cls, item: dict[str, Any], category: Category | None = None) -> Self:
        base = Publication.from_api(item, category)
        return cls(
            **{f.name: getattr(base, f.name) for f in fields(Publication)},
            previous_code=item.get("codigoAnterior"),
            full_text_html=item.get("textoCompleto"),
            archive_path=item.get("archivo"),
            document_b64=item.get("documento"),
        )


@dataclass(slots=True)
class FailedRecord(CsvRow):
    """Row of failed.csv."""

    id: str
    type_code: str
    code: str
    company_name: str
    matricula: str
    matricula_anterior: str
    publication_date: str
    reason: str
    failed_at: str
    attempts: int = 1


    @classmethod
    def from_publication(cls, publication: Publication, reason: str, failed_at: datetime) -> Self:
        return cls(
            id=publication.id,
            type_code=publication.category.code if publication.category else "",
            code=publication.code,
            company_name=publication.company_name,
            matricula=publication.matricula,
            matricula_anterior=publication.matricula_anterior or "",
            publication_date=format_date(publication.published_at),
            reason=reason,
            failed_at=failed_at.isoformat(timespec="seconds"),
        )

    @classmethod
    def from_row(cls, row: dict[str, str]) -> Self:
        return cls(**{**row, "attempts": int(row.get("attempts") or 1)})

    def to_publication(self, category: Category) -> Publication:
        return Publication(
            id=self.id,
            code=self.code,
            title="",
            summary="",
            published_at=parse_date(self.publication_date),
            company_name=self.company_name,
            matricula=self.matricula,
            matricula_anterior=self.matricula_anterior or None,
            category=category,
        )


@dataclass(frozen=True, slots=True)
class CsvRecord(CsvRow):
    """Row of the result CSV required by the task."""

    company_name: str
    matricula: str
    publication_date: str
    file_name: str
    scraped_at: str


    @classmethod
    def from_publication(cls, publication: Publication, file_name: str, scraped_at: datetime) -> Self:
        return cls(
            company_name=publication.company_name,
            matricula=publication.matricula_display,
            publication_date=format_date(publication.published_at),
            file_name=file_name,
            scraped_at=scraped_at.isoformat(timespec="seconds"),
        )


@dataclass(frozen=True, slots=True)
class MetadataRecord(CsvRow):
    """Row of metadata.csv: every publication without downloading it, to prove the collection is complete."""

    id: str
    type_code: str
    type_name: str
    code: str
    publication_date: str
    company_name: str
    matricula: str
    matricula_anterior: str
    title: str
    file_name: str
    scraped_at: str


    @classmethod
    def from_publication(cls, publication: Publication, file_name: str, scraped_at: datetime) -> Self:
        category = publication.category
        return cls(
            id=publication.id,
            type_code=category.code if category else "",
            type_name=category.name if category else "",
            code=publication.code,
            publication_date=format_date(publication.published_at),
            company_name=publication.company_name,
            matricula=publication.matricula,
            matricula_anterior=publication.matricula_anterior or "",
            title=publication.title,
            file_name=file_name,
            scraped_at=scraped_at.isoformat(timespec="seconds"),
        )

    @classmethod
    def from_row(cls, row: dict[str, str]) -> Self:
        return cls(**row)

    def to_publication(self) -> Publication:
        return Publication(
            id=self.id,
            code=self.code,
            title=self.title,
            summary="",
            published_at=parse_date(self.publication_date),
            company_name=self.company_name,
            matricula=self.matricula,
            matricula_anterior=self.matricula_anterior or None,
            category=Category(self.type_code, self.type_name),
        )
