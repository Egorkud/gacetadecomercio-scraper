import csv
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, Self

from src.logger import LoggerMixin

# Spreadsheet apps evaluate cells starting with these as formulas.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return f"'{value}"
    return value


def unescape_cell(value: str) -> str:
    if value.startswith("'") and value[1:].startswith(FORMULA_PREFIXES):
        return value[1:]
    return value


def escape_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: escape_cell(value) for key, value in row.items()}


class CsvService(LoggerMixin):
    # utf-8-sig so Excel shows Spanish characters correctly.
    ENCODING = "utf-8-sig"

    def __init__(self, path: Path, fieldnames: Sequence[str]) -> None:
        self.path = path
        self.fieldnames = list(fieldnames)
        self.rows_written = 0
        self._file: IO[str] | None = None
        self._writer: csv.DictWriter | None = None

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        # The BOM goes only at the start of a new file, appends use plain utf-8.
        self._file = self.path.open("a", encoding=self.ENCODING if is_new else "utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self.fieldnames, extrasaction="ignore")
        if is_new:
            self._writer.writeheader()
        self.logger.info("Writing CSV to %s", self.path)

    def close(self) -> None:
        if self._file and not self._file.closed:
            self._file.close()
            self.logger.info("CSV closed: %d rows written to %s", self.rows_written, self.path)

    def write_row(self, row: Mapping[str, Any]) -> None:
        self.write_rows([row])

    def write_rows(self, rows: Iterable[Mapping[str, Any]]) -> int:
        if self._writer is None or self._file is None:
            raise RuntimeError("CsvService is not open")
        count = 0
        for row in rows:
            self._writer.writerow(escape_row(row))
            count += 1
        # fsync so every written row survives a crash or power loss.
        self._file.flush()
        os.fsync(self._file.fileno())
        self.rows_written += count
        return count

    @classmethod
    def rewrite(cls, path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
        """Replace the whole file atomically, so a crash never leaves it half-written."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.part")
        with tmp_path.open("w", encoding=cls.ENCODING, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(fieldnames))
            writer.writeheader()
            writer.writerows(escape_row(row) for row in rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    @classmethod
    def read_rows(cls, path: Path) -> list[dict[str, str]]:
        with path.open(encoding=cls.ENCODING, newline="") as f:
            return [{key: unescape_cell(value or "") for key, value in row.items()} for row in csv.DictReader(f)]

    @classmethod
    def merge(cls, paths: Iterable[Path], output: Path, key: str | None = None) -> int:
        """Merge run CSVs into one file; optionally de-duplicate by `key` column."""
        seen: set[str] = set()
        fieldnames: list[str] | None = None
        rows: list[dict[str, str]] = []
        for path in paths:
            for row in cls.read_rows(path):
                fieldnames = fieldnames or list(row)
                if key:
                    if row[key] in seen:
                        continue
                    seen.add(row[key])
                rows.append(row)
        if fieldnames is None:
            return 0
        with cls(output, fieldnames) as service:
            return service.write_rows(rows)
