from datetime import datetime
from pathlib import Path

from src.csv_service import CsvService
from src.logger import LoggerMixin
from src.models import FailedRecord, Publication


class FailedRegistry(LoggerMixin):
    """Publications the API could not serve; shared across runs so they can be retried."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, FailedRecord] = {}
        if path.exists():
            self._records = {row["id"]: FailedRecord.from_row(row) for row in CsvService.read_rows(path)}
            self.logger.info("Loaded %d failed publications from %s", len(self._records), path)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, publication_id: str) -> bool:
        return publication_id in self._records

    def records(self) -> list[FailedRecord]:
        return list(self._records.values())

    def add(self, publication: Publication, reason: str) -> None:
        now = datetime.now()
        record = FailedRecord.from_publication(publication, reason, now)
        if previous := self._records.get(publication.id):
            record.attempts = previous.attempts + 1
        self._records[publication.id] = record
        self._save()

    def remove(self, publication_id: str) -> None:
        if self._records.pop(publication_id, None) is not None:
            self._save()

    def _save(self) -> None:
        CsvService.rewrite(self.path, FailedRecord.fieldnames(), (r.to_row() for r in self._records.values()))
