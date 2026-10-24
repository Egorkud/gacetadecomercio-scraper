"""Shows why the scraper never paginates through a big date range.

The API sorts only by `fechaPublicacion`. Rows with the same timestamp come back
in random order, so the next page can repeat rows of the previous one and some
rows are never returned. Old days are the worst case: the whole day has one timestamp.
The scraper splits date ranges until every request fits into one page of 100.

Run: uv run python -m research.pagination_stability_check  (about 6 min)

Result on 2026-10-24:
    2019-03-14 (84 rows, 1 timestamp), pages of 10: only 62 and 55 unique rows, 22-29 lost;
    2022-06-15 (215 rows, 99 timestamps on page 1), pages of 100: same set twice, different order.
"""
from research.common import ResearchApi, date_filter, save_results

# (day, page size): a day with one shared timestamp and a busy day with many.
CASES = [("2019-03-14", 10), ("2022-06-15", 100)]


def check_day(api: ResearchApi, day: str, page_size: int) -> dict:
    whole_day = api.search(limit=100, **date_filter(day))
    timestamps = {row["fechaPublicacion"] for row in whole_day["filas"]}
    # Only a day that fits into one page gives a reliable reference set.
    reference = {row["id"] for row in whole_day["filas"]} if whole_day["total"] <= 100 else None

    runs = [[row["id"] for row in api.search_all(limit=page_size, **date_filter(day))] for _ in range(2)]
    result = {
        "total": whole_day["total"],
        "page_size": page_size,
        "timestamps_on_first_page": len(timestamps),
        "rows_per_run": [len(ids) for ids in runs],
        "unique_per_run": [len(set(ids)) for ids in runs],
        "same_order": runs[0] == runs[1],
        "same_set": set(runs[0]) == set(runs[1]),
    }
    if reference is not None:
        result["lost_per_run"] = [len(reference - set(ids)) for ids in runs]
    print(f"{day}, pages of {page_size}: {result}")
    return result


def main() -> None:
    with ResearchApi() as api:
        results = {day: check_day(api, day, page_size) for day, page_size in CASES}
    print(f"\nSaved to {save_results('pagination_stability_check', results)}")


if __name__ == "__main__":
    main()
