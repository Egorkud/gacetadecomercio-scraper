import argparse
import logging
from datetime import date, datetime, timedelta

from src.complete_set import CompleteSetBuilder
from src.config import Settings
from src.csv_service import CsvService
from src.failed_registry import FailedRegistry
from src.file_service import FileService
from src.gaceta_scraper import GacetaScraper
from src.logger import setup_logging
from src.models import CsvRecord, MetadataRecord

RUN_ID_FORMAT = "%Y-%m-%d_%H-%M-%S"


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scraper for gacetadecomercio.gob.bo publications")
    parser.add_argument("--limit", type=int, help="max publications per type")
    parser.add_argument("--days", type=int, help="only publications from the last N days")
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat, help="start date, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, help="end date, YYYY-MM-DD")
    parser.add_argument("--categories", nargs="+", default=[], help="type codes, e.g. 1001 2001")
    parser.add_argument("--all-types", action="store_true", help="also collect types hidden from the site catalog")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--retry-failed", action="store_true", help="only retry publications listed in failed.csv")
    mode.add_argument("--metadata-only", action="store_true", help="write metadata.csv without downloading files")
    mode.add_argument("--merge", action="store_true", help="merge run CSVs into a checked publications_complete.csv")
    parser.add_argument("--search-interval", type=float, help="min seconds between search requests")
    parser.add_argument("--workers", type=positive_int, help="parallel downloads, 20 by default")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    if args.days is not None and args.date_from is not None:
        parser.error("--days and --from are mutually exclusive")
    return args


def build_settings(args: argparse.Namespace) -> Settings:
    options = {
        "date_to": args.date_to,
        "limit_per_type": args.limit,
        "categories": tuple(args.categories),
        "all_types": args.all_types,
        "metadata_only": args.metadata_only,
        "log_level": args.log_level,
    }
    if args.date_from is not None:
        options["date_from"] = args.date_from
    if args.days is not None:
        options["date_from"] = (args.date_to or date.today()) - timedelta(days=args.days - 1)
    if args.search_interval is not None:
        options["search_interval"] = args.search_interval
    if args.workers is not None:
        options["download_workers"] = args.workers
    return Settings(**options)


def main() -> None:
    args = parse_args()
    settings = build_settings(args)
    run_id = datetime.now().strftime(RUN_ID_FORMAT)

    setup_logging(settings.logs_dir / f"scraper_{run_id}.log", settings.log_level)
    logger = logging.getLogger("main")
    logger.info("Run %s started with %s", run_id, settings)

    failed = FailedRegistry(settings.failed_path)
    if args.merge:
        CompleteSetBuilder(settings, failed).build()
        return

    if settings.metadata_only:
        csv_path, record_type = settings.output_dir / f"metadata_{run_id}.csv", MetadataRecord
    else:
        csv_path, record_type = settings.output_dir / f"publications_{run_id}.csv", CsvRecord
    file_service = FileService(settings.files_dir)
    try:
        with (
            CsvService(csv_path, record_type.fieldnames()) as csv_service,
            GacetaScraper(settings, csv_service, file_service, failed) as scraper,
        ):
            if args.retry_failed:
                scraper.retry_failed()
            else:
                scraper.run()
    except KeyboardInterrupt:
        logger.warning("Interrupted by user, progress is saved")


if __name__ == "__main__":
    main()
