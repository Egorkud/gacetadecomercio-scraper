"""Quick check of models, file and CSV services without touching the API.

Run: uv run python -m research.offline_smoke
Files are written to research/results/offline_smoke/ and recreated on every run.
"""
import base64
import shutil
from datetime import datetime

from research.common import RESULTS_DIR
from src.csv_service import CsvService
from src.failed_registry import FailedRegistry
from src.file_service import FileService, FileServiceError
from src.models import Category, CsvRecord, PublicationDetail

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

    for run in (1, 2):
        with CsvService(OUTPUT_DIR / f"publications_{run}.csv", CsvRecord.fieldnames()) as csv_file:
            csv_file.write_rows([row, formula_row])

    merged = OUTPUT_DIR / "merged.csv"
    assert CsvService.merge(sorted(OUTPUT_DIR.glob("publications_*.csv")), merged) == 4
    raw_text = merged.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in raw_text, "formula was not escaped"
    assert CsvService.read_rows(merged)[1]["company_name"] == formula_row["company_name"]
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


def main() -> None:
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    detail = check_models()
    file_name = check_files(detail)
    check_csv(detail, file_name)
    check_failed_registry(detail)
    print("\nAll checks passed")


if __name__ == "__main__":
    main()
