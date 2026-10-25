import base64
import binascii
import os
from collections.abc import Sequence
from pathlib import Path

from fpdf import FPDF

from src.helpers.utils import clean_filename_part, html_to_text, safe_filename, to_latin1
from src.logger import LoggerMixin


class FileServiceError(Exception):
    pass


class FileService(LoggerMixin):
    MAX_STEM_LENGTH = 180
    MAX_DIR_LENGTH = 100

    def __init__(self, files_dir: Path) -> None:
        self.files_dir = files_dir

    def build_filename(self, *parts: str, extension: str = "pdf") -> str:
        """Join non-empty parts with "_", e.g. "COMPANY S.A._123_456-2026.pdf"."""
        stem = "_".join(clean_filename_part(part) for part in parts if part)
        return f"{safe_filename(stem, self.MAX_STEM_LENGTH)}.{extension}"

    def build_path(self, file_name: str, subdir: str | None = None) -> Path:
        directory = self.files_dir / safe_filename(subdir, self.MAX_DIR_LENGTH) if subdir else self.files_dir
        return directory / file_name

    @staticmethod
    def type_folder(code: str, name: str) -> str:
        return f"{code} - {name}"

    @staticmethod
    def type_code(file_name: str) -> str:
        """Type code of a file name relative to files_dir: "1009 - Name/file.pdf" -> "1009"."""
        return file_name.split("/", 1)[0].split(" - ", 1)[0]

    def exists(self, path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    def save_base64_pdf(self, path: Path, document_b64: str) -> Path:
        try:
            data = base64.b64decode(document_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FileServiceError(f"Invalid base64 document for {path.name}") from exc
        if not data.startswith(b"%PDF"):
            raise FileServiceError(f"Decoded document is not a PDF: {path.name}")
        return self.save_bytes(path, data)

    def save_text_as_pdf(
        self,
        path: Path,
        title: str,
        body: str,
        is_html: bool = False,
        meta: Sequence[str] = (),
    ) -> Path:
        """Render text into a PDF; HTML that fpdf cannot handle falls back to plain text."""
        if is_html:
            try:
                return self.save_bytes(path, self._render_pdf(title, body, meta, is_html=True))
            except Exception as exc:
                self.logger.warning("HTML render failed for %s (%s), using plain text", path.name, exc)
                body = html_to_text(body)
        return self.save_bytes(path, self._render_pdf(title, body, meta))

    def save_bytes(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A temp file first, so an interrupted run never leaves a partial file.
        tmp_path = path.with_name(f"{path.name}.part")
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
        self.logger.debug("Saved %s (%d bytes)", path, len(data))
        return path

    def _render_pdf(self, title: str, body: str, meta: Sequence[str], is_html: bool = False) -> bytes:
        pdf = FPDF(format="A4")
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_title(to_latin1(title))

        pdf.set_font("Helvetica", style="B", size=13)
        pdf.multi_cell(0, 7, to_latin1(title), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=9)
        for line in meta:
            pdf.multi_cell(0, 5, to_latin1(line), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

        pdf.set_font("Helvetica", size=11)
        if is_html:
            pdf.write_html(to_latin1(body))
        else:
            pdf.multi_cell(0, 6, to_latin1(body))
        return bytes(pdf.output())
