# Gaceta de Comercio scraper

Downloads publications from [gacetadecomercio.gob.bo](https://gacetadecomercio.gob.bo) for any period and publication type. Each publication is saved as a PDF, and a CSV lists the company, matrícula, publication date, file name and when it was scraped.

## Quick start

You need Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python main.py --days 7
```

This collects the last week. PDFs land in `output/files/`, the CSV in `output/`, logs in `logs/`.

## How it works

The site has no data of its own: it is a single-page app that loads everything from a public API at `servicios.seprec.gob.bo/api`. The scraper talks to the same API, so it sees exactly what the site shows.

For every publication type from the site's "Publicación" list it searches by date. The API cannot be paged reliably (more on that below), so each search has to fit into one page of 100. Small types fit in one go. Big ones are walked month by month from the newest, and a month that is still too big is cut into shorter date ranges.

Every publication found is then fetched by id: the response carries the PDF, and 20 of these downloads run at once. Some types, such as meeting notices and address changes, have only text; those are turned into a PDF.

The CSV row is written as soon as the file is saved, so stopping the run loses nothing, and the next run skips files that are already there. Publications the server can't return go to `failed.csv` and get another try.

## Why the data is complete

- The scraper reads the same API the site is built on, so there is no second source to miss.
- The overall total matches the sum by publication type, by group and by year. We checked this in `research/api_completeness_check.py`.
- Paging is where data gets lost: rows with the same timestamp come back in random order, so pages overlap and skip rows (`research/pagination_stability_check.py`). The scraper doesn't page. On the rare day with more than 100 publications of one type, it pages that day again and again until it has seen every id.
- Each run compares what it collected for every type with the total the API reported at the start. The log ends with `Complete: every type matches the API total`, or names the type that is short and by how much.
- `--merge` checks that every file from the CSVs is on disk and is a real PDF.

## Recommended workflow

For a large period it is faster to collect the list first and download afterwards:

```bash
# list every publication without downloading anything (about 11 hours for all years)
uv run python main.py --metadata-only

# compare it with what is already downloaded, write missing.csv
uv run python main.py --merge

# download what is missing for the period you need, no searching involved
uv run python main.py --from-missing --from 2026-07-25 --to 2026-10-24

# try the failed ones again and rebuild the final CSV
uv run python main.py --retry-failed
uv run python main.py --merge
```

The plain `uv run python main.py` does everything in a single pass: it searches and downloads at the same time.

## Modes

A run uses one mode at most. Without a mode, it searches and downloads.

| Flag | What it does |
|---|---|
| `--metadata-only` | Lists publications into `metadata_<run>.csv`, downloads nothing |
| `--merge` | Combines all run CSVs into `publications_complete.csv`, checks the files, writes `missing.csv` |
| `--from-missing` | Downloads publications from `missing.csv` by id |
| `--retry-failed` | Tries the publications in `failed.csv` again |

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--from 2026-08-01` | 2017-01-01 | First publication date |
| `--to 2026-09-30` | today | Last publication date |
| `--days 30` | | Last N days, instead of `--from` |
| `--categories 1001 2001` | the 24 types of the task | Only these type codes |
| `--all-types` | off | Also the 4 old types the site no longer lists |
| `--limit 5` | | At most N publications per type, handy for a test |
| `--workers 10` | 20 | Parallel downloads |
| `--search-interval 10` | 8 | Seconds between searches |
| `--log-level DEBUG` | INFO | Log detail |

A few combinations:

```bash
uv run python main.py --from 2026-08-01 --to 2026-09-30 --categories 1003 2002
uv run python main.py --limit 5 --all-types
uv run python main.py --metadata-only --days 30
uv run python main.py --from-missing --categories 1009 --workers 10
```

## Output

| Path | What's inside |
|---|---|
| `output/files/` | PDFs, a folder per type, named `<company>_<matrícula>_<code>.pdf` |
| `output/publications_<run>.csv` | What a single run saved |
| `output/publications_complete.csv` | All runs together, checked against the files |
| `output/metadata_<run>.csv` | Every publication found, from `--metadata-only` |
| `output/missing.csv` | Listed in the metadata but not downloaded yet |
| `output/failed.csv` | Publications the server couldn't return, usually HTTP 412 |
| `logs/scraper_<run>.log` | Run log with the completeness check at the end |

## Project structure

```
main.py                  command line: parses flags and starts the chosen mode
src/
  config.py              all settings and request headers
  scraper.py             HTTP client: rate limiting, retries, Cloudflare handling
  gaceta_scraper.py      the site logic: types, date splitting, downloads
  download_pool.py       worker threads for parallel downloads
  models.py              publications and the rows of every CSV
  file_service.py        file names, folders and saving PDFs
  csv_service.py         crash-safe CSV writing
  failed_registry.py     failed.csv
  complete_set.py        --merge: final CSV and file checks
  logger.py              logging to console and file
  helpers/utils.py       small general helpers: dates, text, file names
research/                scripts used to study the API
```

## Good to know

- Cloudflare blocks the IP for 30 minutes after more than about 10 searches a minute, which is why searches wait 8 seconds between each other. Downloads have no such limit.
- `research/` keeps the scripts we used to study the API, its paging and its limits.
