import base64
import binascii
import os
import re
from pathlib import Path

from fpdf import FPDF

from src.logger import LoggerMixin

INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACES_RE = re.compile(r"\s+")
TYPOGRAPHIC_CHARS = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
})


class FileServiceError(Exception):
    pass


class FileService(LoggerMixin):
    MAX_STEM_LENGTH = 180

    def __init__(self, files_dir: Path) -> None:
        self.files_dir = files_dir

    @staticmethod
    def sanitize(value: str, max_length: int | None = None) -> str:
        value = INVALID_CHARS_RE.sub("-", value)
        value = SPACES_RE.sub(" ", value).strip(" .")
        if max_length:
            value = value[:max_length].rstrip(" .")
        return value or "unknown"

    def build_filename(self, *parts: str, extension: str = "pdf") -> str:
        stem = "_".join(INVALID_CHARS_RE.sub("-", part).strip() for part in parts if part)
        return f"{self.sanitize(stem, self.MAX_STEM_LENGTH)}.{extension}"

    def build_path(self, file_name: str, subdir: str | None = None) -> Path:
        directory = self.files_dir / self.sanitize(subdir, 100) if subdir else self.files_dir
        return directory / file_name

    def exists(self, path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    def save_bytes(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file first so an interrupted run never leaves a partial file.
        tmp_path = path.with_name(f"{path.name}.part")
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
        self.logger.debug("Saved %s (%d bytes)", path, len(data))
        return path

    def save_base64_pdf(self, path: Path, document_b64: str) -> Path:
        try:
            data = base64.b64decode(document_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FileServiceError(f"Invalid base64 document for {path.name}") from exc
        if not data.startswith(b"%PDF"):
            raise FileServiceError(f"Decoded document is not a PDF: {path.name}")
        return self.save_bytes(path, data)

    def save_text_as_pdf(self, path: Path, title: str, body: str, is_html: bool = False) -> Path:
        pdf = FPDF(format="A4")
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_title(self._to_latin1(title))

        pdf.set_font("Helvetica", style="B", size=13)
        pdf.multi_cell(0, 7, self._to_latin1(title), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

        pdf.set_font("Helvetica", size=11)
        if is_html:
            pdf.write_html(self._to_latin1(body))
        else:
            pdf.multi_cell(0, 6, self._to_latin1(body))

        return self.save_bytes(path, bytes(pdf.output()))

    @staticmethod
    def _to_latin1(text: str) -> str:
        # Built-in PDF fonts only cover latin-1, which is enough for Spanish.
        return text.translate(TYPOGRAPHIC_CHARS).encode("latin-1", "replace").decode("latin-1")
