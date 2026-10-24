import argparse
import logging
from dataclasses import replace
from datetime import datetime

from src.config import Settings
from src.logger import setup_logging

RUN_ID_FORMAT = "%Y-%m-%d_%H-%M-%S"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scraper for gacetadecomercio.gob.bo publications")
    parser.add_argument("--max-pages", type=int, help="max result pages per category")
    parser.add_argument("--days", type=int, help="only publications from the last N days")
    parser.add_argument("--categories", nargs="+", default=[], help="category codes, e.g. 1001 2001")
    parser.add_argument("--min-interval", type=float, help="min seconds between requests")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings(
        max_pages=args.max_pages,
        days=args.days,
        categories=tuple(args.categories),
        log_level=args.log_level,
    )
    if args.min_interval is not None:
        settings = replace(settings, min_request_interval=args.min_interval)
    return settings


def main() -> None:
    args = parse_args()
    settings = build_settings(args)
    run_id = datetime.now().strftime(RUN_ID_FORMAT)

    setup_logging(settings.logs_dir / f"scraper_{run_id}.log", settings.log_level)
    logger = logging.getLogger("main")
    logger.info("Run %s started with %s", run_id, settings)

    csv_path = settings.output_dir / f"publications_{run_id}.csv"
    logger.info("CSV: %s | files: %s", csv_path, settings.files_dir)
    # TODO: run GacetaScraper once implemented.


if __name__ == "__main__":
    main()
