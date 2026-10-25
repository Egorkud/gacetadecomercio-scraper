from dataclasses import dataclass, field
from datetime import datetime

from src.config import Settings
from src.csv_service import CsvService
from src.failed_registry import FailedRegistry
from src.logger import LoggerMixin
from src.models import CsvRecord, MetadataRecord

MISSING_FILE = "file missing on disk"
BROKEN_FILE = "file is not a valid PDF"


@dataclass(slots=True)
class MergeReport:
    csv_files: int = 0
    rows_read: int = 0
    unique_rows: int = 0
    files_ok: int = 0
    files_missing: int = 0
    files_broken: int = 0
    orphans_restored: int = 0
    not_in_metadata: int = 0
    added_to_failed: int = 0
    metadata_rows: int = 0
    not_downloaded: int = 0
    unknown: list[str] = field(default_factory=list)


class CompleteSetBuilder(LoggerMixin):
    """Merges all run CSVs into publications_complete.csv, checking every file on disk.

    Missing or broken files go to failed.csv for --retry-failed. With metadata_*.csv present,
    orphan files get their rows back and missing.csv lists what is not downloaded yet.
    """

    def __init__(self, settings: Settings, failed: FailedRegistry) -> None:
        self.settings = settings
        self.failed = failed
        self.report = MergeReport()

    def build(self) -> MergeReport:
        rows = self._read_runs()
        metadata = self._read_metadata()

        complete = {}
        for file_name, row in rows.items():
            if problem := self._check_file(file_name):
                self._mark_failed(file_name, problem, metadata)
            else:
                complete[file_name] = row
        self.report.files_ok = len(complete)
        self._restore_orphans(complete, metadata)
        if metadata:
            self._check_names(complete, metadata)

        newest_first = sorted(complete.values(), key=lambda r: (r["publication_date"], r["file_name"]), reverse=True)
        CsvService.rewrite(self.settings.complete_path, CsvRecord.fieldnames(), newest_first)
        if metadata:
            self._write_missing(complete, metadata)
        self._log_report()
        return self.report

    def _read_runs(self) -> dict[str, dict[str, str]]:
        """Rows of every publications_*.csv by file name; a repeated row keeps its latest scrape."""
        paths = sorted(self.settings.output_dir.glob("publications_*.csv"))
        paths = [path for path in paths if path != self.settings.complete_path]
        self.report.csv_files = len(paths)
        rows = {}
        for path in paths:
            for row in CsvService.read_rows(path):
                self.report.rows_read += 1
                current = rows.get(row["file_name"])
                if current is None or row["scraped_at"] > current["scraped_at"]:
                    rows[row["file_name"]] = row
        self.report.unique_rows = len(rows)
        return rows

    def _read_metadata(self) -> dict[str, MetadataRecord]:
        records = {}
        for path in sorted(self.settings.output_dir.glob("metadata_*.csv")):
            for row in CsvService.read_rows(path):
                record = MetadataRecord.from_row(row)
                current = records.get(record.file_name)
                if current is None or record.scraped_at > current.scraped_at:
                    records[record.file_name] = record
        self.report.metadata_rows = len(records)
        return records

    def _check_file(self, file_name: str) -> str | None:
        path = self.settings.files_dir / file_name
        if not path.is_file():
            return MISSING_FILE
        with path.open("rb") as f:
            if f.read(4) != b"%PDF":
                return BROKEN_FILE
        return None

    def _mark_failed(self, file_name: str, reason: str, metadata: dict[str, MetadataRecord]) -> None:
        if reason == BROKEN_FILE:
            self.report.files_broken += 1
            # The scraper skips existing files, so a broken one is moved aside to be downloaded again.
            path = self.settings.files_dir / file_name
            path.replace(path.with_name(f"{path.name}.broken"))
        else:
            self.report.files_missing += 1

        # failed.csv needs the publication id, which only metadata has.
        if record := metadata.get(file_name):
            self.failed.add(record.to_publication(), reason)
            self.report.added_to_failed += 1
        else:
            self.report.unknown.append(file_name)
            self.logger.warning("%s: %s, no metadata to retry it", file_name, reason)

    def _restore_orphans(self, complete: dict[str, dict[str, str]], metadata: dict[str, MetadataRecord]) -> None:
        """Files without a CSV row, e.g. after a crash between saving the file and writing the row."""
        for path in self.settings.files_dir.rglob("*.pdf"):
            file_name = path.relative_to(self.settings.files_dir).as_posix()
            if file_name in complete:
                continue
            if problem := self._check_file(file_name):
                self._mark_failed(file_name, problem, metadata)
            elif record := metadata.get(file_name):
                scraped_at = datetime.fromtimestamp(path.stat().st_mtime)
                complete[file_name] = CsvRecord.from_publication(record.to_publication(), file_name, scraped_at).to_row()
                self.report.orphans_restored += 1
            else:
                self.report.unknown.append(file_name)
                self.logger.warning("%s: file has no CSV row and no metadata", file_name)

    def _check_names(self, complete: dict[str, dict[str, str]], metadata: dict[str, MetadataRecord]) -> None:
        """Downloaded files of types covered by metadata must have the same name there."""
        covered_types = {record.type_code for record in metadata.values()}
        for file_name in complete:
            type_code = file_name.split("/", 1)[0].split(" - ", 1)[0]  # "1009 - Name/file.pdf"
            if file_name not in metadata and type_code in covered_types:
                self.report.not_in_metadata += 1
                # Usually a file saved under an older naming rule; it would be downloaded again.
                self.logger.warning("%s: downloaded file is not in metadata", file_name)

    def _write_missing(self, complete: dict[str, dict[str, str]], metadata: dict[str, MetadataRecord]) -> None:
        missing = [record.to_row() for file_name, record in metadata.items() if file_name not in complete]
        self.report.not_downloaded = len(missing)
        CsvService.rewrite(self.settings.missing_path, MetadataRecord.fieldnames(), missing)

    def _log_report(self) -> None:
        report = self.report
        self.logger.info(
            "Merged %d CSV files: %d rows -> %d unique",
            report.csv_files, report.rows_read, report.unique_rows,
        )
        self.logger.info(
            "Files: %d ok, %d missing, %d broken, %d orphans restored from metadata; %d added to %s",
            report.files_ok, report.files_missing, report.files_broken, report.orphans_restored,
            report.added_to_failed, self.failed.path,
        )
        if report.metadata_rows:
            self.logger.info(
                "Against metadata: %d publications, %d not downloaded yet, see %s",
                report.metadata_rows, report.not_downloaded, self.settings.missing_path,
            )
        else:
            self.logger.info("No metadata_*.csv found, completeness against the API is not checked")
        if report.not_in_metadata:
            self.logger.warning("%d downloaded files are not in metadata, see the warnings above", report.not_in_metadata)
        if report.unknown:
            self.logger.warning("%d files could not be matched, see the warnings above", len(report.unknown))
        self.logger.info("Complete set saved to %s", self.settings.complete_path)
