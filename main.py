import argparse
import logging
from datetime import date, datetime, timedelta

from src.config import Settings
from src.csv_service import CsvService
from src.failed_registry import FailedRegistry
from src.file_service import FileService
from src.gaceta_scraper import GacetaScraper
from src.logger import setup_logging
from src.models import CsvRecord

RUN_ID_FORMAT = "%Y-%m-%d_%H-%M-%S"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scraper for gacetadecomercio.gob.bo publications")
    parser.add_argument("--limit", type=int, help="max publications per type")
    parser.add_argument("--days", type=int, help="only publications from the last N days")
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat, help="start date, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, help="end date, YYYY-MM-DD")
    parser.add_argument("--categories", nargs="+", default=[], help="type codes, e.g. 1001 2001")
    parser.add_argument("--all-types", action="store_true", help="also collect types hidden from the site catalog")
    parser.add_argument("--retry-failed", action="store_true", help="only retry publications listed in failed.csv")
    parser.add_argument("--min-interval", type=float, help="min seconds between requests")
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
        "log_level": args.log_level,
    }
    if args.date_from is not None:
        options["date_from"] = args.date_from
    if args.days is not None:
        options["date_from"] = (args.date_to or date.today()) - timedelta(days=args.days - 1)
    if args.min_interval is not None:
        options["min_request_interval"] = args.min_interval
    return Settings(**options)


def main() -> None:
    args = parse_args()
    settings = build_settings(args)
    run_id = datetime.now().strftime(RUN_ID_FORMAT)

    setup_logging(settings.logs_dir / f"scraper_{run_id}.log", settings.log_level)
    logger = logging.getLogger("main")
    logger.info("Run %s started with %s", run_id, settings)

    csv_path = settings.output_dir / f"publications_{run_id}.csv"
    file_service = FileService(settings.files_dir)
    failed = FailedRegistry(settings.failed_path)
    try:
        with (
            CsvService(csv_path, CsvRecord.fieldnames()) as csv_service,
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
