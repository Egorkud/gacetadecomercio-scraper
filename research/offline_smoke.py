"""Quick check of models, file and CSV services and the CSV merge without touching the API.

Run: uv run python -m research.offline_smoke
Files are written to research/results/offline_smoke/ and recreated on every run.
"""
import base64
import shutil
from datetime import datetime

from research.common import RESULTS_DIR
from src.complete_set import CompleteSetBuilder
from src.config import Settings
from src.csv_service import CsvService
from src.failed_registry import FailedRegistry
from src.file_service import FileService, FileServiceError
from src.models import Category, CsvRecord, MetadataRecord, PublicationDetail

OUTPUT_DIR = RESULTS_DIR / "offline_smoke"

CATEGORY = Category(code="2001", name="Convocatoria a Asamblea/Junta General")

# Shortened detail response of a real text publication.
DETAIL = {
    "id": "228476",
    "codigo": "123366/2026",
    "codigoAnterior": None,
    "titulo": "CONVOCATORIA “JUNTA GENERAL”",
    "resumen": "Se convoca a los socios...",
    "textoCompleto": "<h3>CONVOCATORIA</h3><p>Señor Núñez: dirección — calle 1</p>",
    "fechaPublicacion": "2026-10-24T08:00:00.000Z",
    "fechaPublicacionFormato": "2026/10/24",
    "empresa": {"matricula": "375878027", "matriculaAnterior": "00408951", "razonSocial": "TORRE \"PALADIUM\" S.A."},
    "documento": None,
}


def check_models() -> PublicationDetail:
    detail = PublicationDetail.from_api(DETAIL, CATEGORY)
    assert detail.published_at.isoformat() == "2026-10-24"
    assert detail.matricula_display == "375878027 (00408951)"
    assert not detail.has_document
    print("models: ok")
    return detail


def check_files(detail: PublicationDetail) -> str:
    files = FileService(OUTPUT_DIR / "files")
    file_name = files.build_filename(detail.company_name, detail.matricula, detail.code)
    assert file_name == "TORRE PALADIUM S.A._375878027_123366-2026.pdf", file_name

    path = files.build_path(file_name, f"{CATEGORY.code} - {CATEGORY.name}")
    files.save_text_as_pdf(path, detail.title, detail.full_text_html, is_html=True, meta=[f"Código: {detail.code}"])
    assert path.read_bytes().startswith(b"%PDF")

    try:
        files.save_base64_pdf(files.build_path("not_a_pdf.pdf"), base64.b64encode(b"hello").decode())
    except FileServiceError:
        pass
    else:
        raise AssertionError("save_base64_pdf accepted a file that is not a PDF")
    print(f"files: ok ({path.relative_to(OUTPUT_DIR)})")
    return file_name


def check_csv(detail: PublicationDetail, file_name: str) -> None:
    row = CsvRecord.from_publication(detail, file_name, datetime.now()).to_row()
    formula_row = {**row, "company_name": "=HYPERLINK(\"http://example.com\")"}

    path = OUTPUT_DIR / "publications.csv"
    with CsvService(path, CsvRecord.fieldnames()) as csv_file:
        csv_file.write_rows([row, formula_row])

    assert "'=HYPERLINK" in path.read_text(encoding="utf-8-sig"), "formula was not escaped"
    assert CsvService.read_rows(path)[1]["company_name"] == formula_row["company_name"]
    print("csv: ok")


def check_failed_registry(detail: PublicationDetail) -> None:
    path = OUTPUT_DIR / "failed.csv"
    registry = FailedRegistry(path)
    registry.add(detail, "HTTP 412: Error al obtener el archivo")
    registry.add(detail, "HTTP 412: Error al obtener el archivo")

    reloaded = FailedRegistry(path)
    assert detail.id in reloaded
    assert reloaded.records()[0].attempts == 2
    reloaded.remove(detail.id)
    assert len(FailedRegistry(path)) == 0
    print("failed registry: ok")


def metadata_record(publication_id: str, name: str) -> MetadataRecord:
    return MetadataRecord(
        id=publication_id, type_code="1009", type_name="Modificación", code=f"{publication_id}/2026",
        publication_date="2026-10-23", company_name=name, matricula="1", matricula_anterior="",
        title="", file_name=f"1009/{name}.pdf", scraped_at="2026-10-25T00:00:00",
    )


def check_complete_set() -> None:
    """good is in two runs, missing has no file, broken is not a PDF, orphan has no CSV row,
    renamed was saved under an old name that metadata does not know."""
    settings = Settings(output_dir=OUTPUT_DIR / "merge")
    names = ("good", "missing", "broken", "orphan", "not_downloaded")
    records = {name: metadata_record(str(number), name) for number, name in enumerate(names, start=1)}

    files_dir = settings.files_dir / "1009"
    files_dir.mkdir(parents=True)
    (files_dir / "good.pdf").write_bytes(b"%PDF-1.4")
    (files_dir / "orphan.pdf").write_bytes(b"%PDF-1.4")
    (files_dir / "broken.pdf").write_bytes(b"<html>")
    (files_dir / "-renamed-.pdf").write_bytes(b"%PDF-1.4")

    def run_row(name: str, scraped_at: str) -> dict[str, str]:
        publication = records[name].to_publication()
        return {**CsvRecord.from_publication(publication, f"1009/{name}.pdf", datetime.now()).to_row(),
                "scraped_at": scraped_at}

    first_run = [run_row(name, "2026-10-24T10:00:00") for name in ("good", "missing", "broken")]
    first_run.append({**first_run[0], "file_name": "1009/-renamed-.pdf"})
    CsvService.rewrite(settings.output_dir / "publications_1.csv", CsvRecord.fieldnames(), first_run)
    CsvService.rewrite(settings.output_dir / "publications_2.csv", CsvRecord.fieldnames(),
                       [run_row("good", "2026-10-24T11:00:00")])
    CsvService.rewrite(settings.output_dir / "metadata_1.csv", MetadataRecord.fieldnames(),
                       [record.to_row() for record in records.values()])

    failed = FailedRegistry(settings.failed_path)
    report = CompleteSetBuilder(settings, failed).build()

    complete = {row["file_name"]: row for row in CsvService.read_rows(settings.complete_path)}
    assert set(complete) == {"1009/good.pdf", "1009/orphan.pdf", "1009/-renamed-.pdf"}, complete
    assert complete["1009/good.pdf"]["scraped_at"] == "2026-10-24T11:00:00"
    assert {record.id for record in failed.records()} == {"2", "3"}
    assert (files_dir / "broken.pdf.broken").exists() and not (files_dir / "broken.pdf").exists()
    missing = {row["file_name"] for row in CsvService.read_rows(settings.missing_path)}
    assert missing == {"1009/missing.pdf", "1009/broken.pdf", "1009/not_downloaded.pdf"}, missing
    assert (report.rows_read, report.unique_rows, report.orphans_restored) == (5, 4, 1)
    assert report.not_in_metadata == 1
    print("complete set: ok")


def main() -> None:
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    detail = check_models()
    file_name = check_files(detail)
    check_csv(detail, file_name)
    check_failed_registry(detail)
    check_complete_set()
    print("\nAll checks passed")


if __name__ == "__main__":
    main()
