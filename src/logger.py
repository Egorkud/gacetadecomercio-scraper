import logging
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_file: Path, level: str = "INFO") -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    # Redirected output on Windows defaults to cp1251, which has no "ó" and breaks logging.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    for noisy in ("curl_cffi", "fontTools", "fpdf", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class LoggerMixin:
    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(type(self).__name__)
