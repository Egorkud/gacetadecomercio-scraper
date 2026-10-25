"""Small general-purpose helpers that know nothing about the site."""
import argparse
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from html import unescape

TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"

QUOTES_RE = re.compile(r"[\"“”«»]")
INVALID_FILENAME_CHARS_RE = re.compile(r"[<>:/\\|?*\x00-\x1f]")
SPACES_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")
NON_WORD_RE = re.compile(r"\W+")

# Built-in PDF fonts only cover latin-1, which is enough for Spanish once these are replaced.
TYPOGRAPHIC_CHARS = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
})


def parse_date(value: str | None) -> date | None:
    """Date from "2026/10/24" or an ISO string; None if it can't be parsed."""
    if not value:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    return None


def format_date(value: date | None) -> str:
    return value.isoformat() if value else ""


def date_ranges(date_from: date, date_to: date, days: int) -> Iterator[tuple[date, date]]:
    """Consecutive ranges of `days` days, newest first."""
    range_to = date_to
    while range_to >= date_from:
        range_from = max(date_from, range_to - timedelta(days=days - 1))
        yield range_from, range_to
        range_to = range_from - timedelta(days=1)


def timestamp(moment: datetime | None = None, precise: bool = False) -> str:
    """Time for file names, e.g. 2026-10-25_18-33-48; `precise` adds microseconds."""
    moment = moment or datetime.now()
    return moment.strftime(f"{TIMESTAMP_FORMAT}-%f" if precise else TIMESTAMP_FORMAT)


def html_to_text(html: str) -> str:
    return SPACES_RE.sub(" ", unescape(TAG_RE.sub(" ", html))).strip()


def to_latin1(text: str) -> str:
    return text.translate(TYPOGRAPHIC_CHARS).encode("latin-1", "replace").decode("latin-1")


def clean_filename_part(value: str) -> str:
    """Drop quotes and replace characters Windows does not allow in file names."""
    return INVALID_FILENAME_CHARS_RE.sub("-", QUOTES_RE.sub("", value)).strip()


def safe_filename(value: str, max_length: int) -> str:
    # Dots are stripped only at the end, so "S.A." inside a name stays intact.
    value = SPACES_RE.sub(" ", clean_filename_part(value)).strip(" .")
    return value[:max_length].rstrip(" .") or "unknown"


def slugify(text: str) -> str:
    return NON_WORD_RE.sub("-", text).strip("-")


def positive_int(value: str) -> int:
    """argparse type for counts that must be at least 1."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number
