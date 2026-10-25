import threading
from datetime import datetime
from pathlib import Path

from src.csv_service import CsvService
from src.logger import LoggerMixin
from src.models import FailedRecord, Publication


class FailedRegistry(LoggerMixin):
    """Publications the API could not serve; shared across runs so they can be retried. Thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, FailedRecord] = {}
        self._lock = threading.Lock()
        if path.exists():
            self._records = {row["id"]: FailedRecord.from_row(row) for row in CsvService.read_rows(path)}
            self.logger.info("Loaded %d failed publications from %s", len(self._records), path)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, publication_id: str) -> bool:
        return publication_id in self._records

    def records(self) -> list[FailedRecord]:
        with self._lock:
            return list(self._records.values())

    def add(self, publication: Publication, reason: str) -> None:
        record = FailedRecord.from_publication(publication, reason, datetime.now())
        with self._lock:
            if previous := self._records.get(publication.id):
                record.attempts = previous.attempts + 1
            self._records[publication.id] = record
            self._save()

    def remove(self, publication_id: str) -> None:
        with self._lock:
            if self._records.pop(publication_id, None) is not None:
                self._save()

    def _save(self) -> None:
        CsvService.rewrite(self.path, FailedRecord.fieldnames(), (r.to_row() for r in self._records.values()))
