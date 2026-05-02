#!/usr/bin/env python3
"""
Scrape SBA Small Business Search listings from the Advanced Search backend API.

Discovered endpoint:
  POST https://search.certifications.sba.gov/_api/v2/search

The frontend submits a JSON body with this filter schema (matching the app store):
  - searchProfiles.searchTerm
  - location.{states,zipCodes,counties,districts,msas}
  - sbaCertifications, naics, selfCertifications, keywords
  - lastUpdated, samStatus, qualityAssuranceStandards
  - bondingLevels, businessSize, annualRevenue
  - entityDetailId
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


API_URL = "https://search.certifications.sba.gov/_api/v2/search"
MAX_RESULTS_WARNING_THRESHOLD = 150_000

HTTP_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://search.certifications.sba.gov",
    "Referer": "https://search.certifications.sba.gov/advanced",
    "User-Agent": "Mozilla/5.0",
}

STATE_PAIRS: List[Tuple[str, str]] = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"), ("DC", "District of Columbia"),
    ("PR", "Puerto Rico"), ("VI", "Virgin Islands"), ("GU", "Guam"),
    ("AS", "American Samoa"), ("MP", "Northern Mariana Islands"),
]

# NAICS prefix buckets aligned to AUOTAM ICP industries.
INDUSTRY_NAICS_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "ecommerce": ("44", "45"),
    "nonprofit": ("81", "92"),
    "housing_real_estate": ("53",),
    "construction": ("23",),
    "healthcare": ("62",),
    "finance": ("52",),
    "government_defense": ("92", "54"),
    "education": ("61",),
    "technology": ("51", "54"),
    "landscape": ("56", "11"),
}

CSV_COLUMNS = [
    "segment",
    "business_name",
    "owner_name",
    "email",
    "phone",
    "address",
    "city",
    "state",
    "website",
    "naics_primary",
    "naics_all_codes",
    "entity_detail_id",
    "uei",
    "cage_code",
]


def default_filters() -> dict:
    return {
        "searchProfiles": {"searchTerm": ""},
        "location": {"states": [], "zipCodes": [], "counties": [], "districts": [], "msas": []},
        "sbaCertifications": {"activeCerts": [], "isPreviousCert": False, "operatorType": "Or"},
        "naics": {"codes": [], "isPrimary": False, "operatorType": "Or"},
        "selfCertifications": {"certifications": [], "operatorType": "Or"},
        "keywords": {"list": [], "operatorType": "Or"},
        "lastUpdated": {"date": {"label": "Anytime", "value": "anytime"}},
        "samStatus": {"isActiveSAM": False},
        "qualityAssuranceStandards": {"qas": []},
        "bondingLevels": {
            "constructionIndividual": "",
            "constructionAggregate": "",
            "serviceIndividual": "",
            "serviceAggregate": "",
        },
        "businessSize": {"relationOperator": "at-least", "numberOfEmployees": ""},
        "annualRevenue": {"relationOperator": "at-least", "annualGrossRevenue": ""},
        "entityDetailId": "",
    }


def post_search(payload: dict, retry: int = 3, sleep_seconds: float = 1.5) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=HTTP_HEADERS,
        method="POST",
    )
    last_error = None
    for attempt in range(1, retry + 1):
        try:
            with urllib.request.urlopen(req, timeout=240) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - keep stdlib-only
            last_error = exc
            if attempt < retry:
                time.sleep(sleep_seconds * attempt)
    raise RuntimeError(f"API request failed after {retry} attempts: {last_error}")


def build_state_filter(state_code: str, state_name: str) -> dict:
    payload = default_filters()
    payload["location"]["states"] = [{"label": f"{state_name} ({state_code})", "value": f"{state_code} - {state_name}"}]
    return payload


def segment_for_naics(naics_primary: str) -> str:
    if not naics_primary:
        return "universal"
    prefix = str(naics_primary)[:2]
    for segment, prefixes in INDUSTRY_NAICS_PREFIXES.items():
        if prefix in prefixes:
            return segment
    return "universal"


def row_from_item(item: dict) -> dict:
    address = " ".join([v for v in [item.get("address_1"), item.get("address_2")] if v]).strip()
    return {
        "business_name": item.get("legal_business_name", "") or "",
        "owner_name": item.get("contact_person", "") or "",
        "email": item.get("email", "") or "",
        "phone": item.get("phone", "") or "",
        "address": address,
        "city": item.get("city", "") or "",
        "state": item.get("state", "") or "",
        "website": item.get("website") or item.get("additional_website") or "",
        "naics_primary": item.get("naics_primary", "") or "",
        "naics_all_codes": ",".join(item.get("naics_all_codes") or []),
        "entity_detail_id": item.get("entity_detail_id", "") or "",
        "uei": item.get("uei", "") or "",
        "cage_code": item.get("cage_code", "") or "",
    }


def scrape_all_states(state_subset: Iterable[Tuple[str, str]] | None = None) -> List[dict]:
    rows: List[dict] = []
    seen_entity_ids = set()
    states = list(state_subset) if state_subset else STATE_PAIRS

    for idx, (state_code, state_name) in enumerate(states, start=1):
        print(f"[{idx}/{len(states)}] Fetching {state_name} ({state_code})...")
        payload = build_state_filter(state_code, state_name)
        data = post_search(payload)
        results = data.get("results", [])
        count = len(results)
        print(f"  -> {count} records")

        if count >= MAX_RESULTS_WARNING_THRESHOLD:
            print(
                f"  !! Warning: {state_code} returned >= {MAX_RESULTS_WARNING_THRESHOLD}. "
                "Potential truncation; split this state further by extra filters if needed."
            )

        for item in results:
            entity_id = item.get("entity_detail_id")
            if entity_id and entity_id in seen_entity_ids:
                continue
            if entity_id:
                seen_entity_ids.add(entity_id)

            base_row = row_from_item(item)
            segment = segment_for_naics(base_row["naics_primary"])
            rows.append({"segment": segment, **base_row})

        # Rate-limit to reduce chance of upstream throttling.
        time.sleep(0.7)

    return rows


def write_segmented_csv(rows: List[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["segment"]].append(row)

    all_path = out_dir / "all_businesses.csv"
    with all_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    file_name_map = {
        "ecommerce": "segment_ecommerce.csv",
        "nonprofit": "segment_nonprofit.csv",
        "housing_real_estate": "segment_housing_realestate.csv",
        "universal": "segment_universal.csv",
    }

    for segment, seg_rows in grouped.items():
        seg_file = file_name_map.get(segment, f"segment_{segment}.csv")
        seg_path = out_dir / seg_file
        with seg_path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(seg_rows)

    print(f"Wrote {len(rows)} rows to {all_path}")
    print(f"Wrote {len(grouped)} segment CSV files to {out_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape SBA business listings into segmented CSV files.")
    parser.add_argument(
        "--out-dir",
        default="output/sba",
        help="Output directory for CSV files (default: output/sba)",
    )
    parser.add_argument(
        "--states",
        default="",
        help="Optional comma-separated state codes to limit run, e.g. TX,CA,NY",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)

    state_subset = None
    if args.states.strip():
        wanted = {s.strip().upper() for s in args.states.split(",") if s.strip()}
        state_subset = [(code, name) for code, name in STATE_PAIRS if code in wanted]
        if not state_subset:
            raise SystemExit("No valid state codes provided in --states")

    rows = scrape_all_states(state_subset=state_subset)
    write_segmented_csv(rows, out_dir=out_dir)


if __name__ == "__main__":
    main()
