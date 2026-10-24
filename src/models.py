from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from typing import Any, Self


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True, slots=True)
class Category:
    code: str
    name: str

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Self:
        return cls(code=str(item["codigoParametrica"]), name=item["nombreParametrica"])


@dataclass(slots=True)
class Publication:
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


@dataclass(frozen=True, slots=True)
class CsvRecord:
    company_name: str
    matricula: str
    publication_date: str
    file_name: str
    scraped_at: str

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def from_publication(cls, publication: Publication, file_name: str, scraped_at: datetime) -> Self:
        return cls(
            company_name=publication.company_name,
            matricula=publication.matricula_display,
            publication_date=publication.published_at.isoformat() if publication.published_at else "",
            file_name=file_name,
            scraped_at=scraped_at.isoformat(timespec="seconds"),
        )

    def to_row(self) -> dict[str, str]:
        return asdict(self)
