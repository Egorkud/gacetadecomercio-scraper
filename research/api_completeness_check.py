"""Checks that the public API gives access to every publication on the site.

1. The "Todas" total must match the sum by groups, by years and by publication types
   (catalog types plus HIDDEN_PUBLICATION_TYPES from src/gaceta_scraper.py).
2. For text-only types the list `resumen` must equal the detail `textoCompleto`,
   otherwise the scraper would need a detail request for each of them.
3. Other endpoints that could help (batch details, another list) are tried and their answers saved.

Run: uv run python -m research.api_completeness_check  (about 15 min)

Result on 2026-10-24:
    total, groups and years all give 228 435; catalog types 219 515 + hidden 8 920 = 228 435;
    resumen == textoCompleto for every checked text publication;
    no batch endpoint (unknown fields like "ids" are ignored), /publicaciones-abiertas answers 500,
    Swagger at /docs-json is open.
"""
import re
from datetime import date
from html import unescape

from research.common import ResearchApi, date_filter, save_results, search_body
from src.gaceta_scraper import HIDDEN_PUBLICATION_TYPES, TEXT_PUBLICATION_TYPES

# The oldest publication is from 2017, so 2015 and 2016 must be empty.
FIRST_YEAR = 2015


def normalize_text(text: str | None) -> str:
    text = unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip().upper()


def compare_totals(api: ResearchApi) -> dict:
    catalog_codes = [item["codigoParametrica"] for item in api.catalog()]

    total = api.total()
    print(f"Todas: {total}")

    groups = {group: api.total(categoria=group) for group in "1234"}
    print(f"Groups: {groups} -> {sum(groups.values())}")

    years = {
        year: api.total(**date_filter(f"{year}-01-01", f"{year}-12-31"))
        for year in range(FIRST_YEAR, date.today().year + 1)
    }
    print(f"Years: {years} -> {sum(years.values())}")

    catalog_types = {code: api.total(tipoPublicacion=code) for code in catalog_codes}
    hidden_types = {code: api.total(tipoPublicacion=code) for code in HIDDEN_PUBLICATION_TYPES}
    types_sum = sum(catalog_types.values()) + sum(hidden_types.values())
    print(f"Catalog types: {sum(catalog_types.values())}, hidden types: {hidden_types} -> {types_sum}")

    all_match = total == sum(groups.values()) == sum(years.values()) == types_sum
    print(f"All totals match: {all_match}\n")
    return {
        "total": total,
        "groups": groups,
        "years": years,
        "catalog_types": catalog_types,
        "hidden_types": hidden_types,
        "all_match": all_match,
    }


def compare_text_types(api: ResearchApi) -> list[dict]:
    results = []
    for code in sorted(TEXT_PUBLICATION_TYPES):
        row = api.search(limit=10, tipoPublicacion=code)["filas"][0]
        detail = api.detail(row["id"])
        summary = normalize_text(row["resumen"])
        full_text = normalize_text(detail.get("textoCompleto"))
        result = {
            "type": code,
            "id": row["id"],
            "resumen_length": len(summary),
            "texto_length": len(full_text),
            "equal": summary == full_text,
            "has_document": bool(detail.get("documento")),
        }
        print(f"Text type {code}: {result}")
        results.append(result)
    print()
    return results


def try_other_endpoints(api: ResearchApi) -> dict:
    two_ids = "228535,228600"
    attempts = {
        "GET publicaciones/<id>,<id>": ("GET", f"publicaciones/{two_ids}", {}),
        "GET publicaciones?ids=": ("GET", f"publicaciones?ids={two_ids}", {}),
        "POST publicaciones with ids": (
            "POST", "publicaciones", {"json": {**search_body(limit=10), "ids": two_ids.split(",")}}
        ),
        "GET publicaciones-abiertas": ("GET", "publicaciones-abiertas?limite=10&pagina=1", {}),
        "GET docs-json": ("GET", "docs-json", {}),
    }
    results = {}
    for name, (method, url, kwargs) in attempts.items():
        response = api.raw(method, url, **kwargs)
        results[name] = {"status": response.status_code, "body": response.text[:300]}
        print(f"{name}: {response.status_code} {response.text[:120]!r}")
    return results


def main() -> None:
    with ResearchApi() as api:
        results = {
            "totals": compare_totals(api),
            "text_types": compare_text_types(api),
            "other_endpoints": try_other_endpoints(api),
        }
    print(f"\nSaved to {save_results('api_completeness_check', results)}")


if __name__ == "__main__":
    main()
