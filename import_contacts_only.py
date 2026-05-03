#!/usr/bin/env python3
"""
Bulk-import contacts from output/sba/all_businesses.csv into PostgreSQL `contacts` only.

Loads DATABASE_URL from .env (project root). Does not send email or touch
sequence_status / email_log.

Usage:
  python3 import_contacts_only.py
  python3 import_contacts_only.py --csv output/sba/all_businesses.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def load_dotenv(path: Path) -> None:
    """Set missing os.environ keys from KEY=VALUE lines in .env (no deps)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def split_name(owner_name: str) -> Tuple[str, str]:
    parts = (owner_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def row_to_tuple(row: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
    email = (row.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return None
    fn, ln = split_name(row.get("owner_name") or "")
    return (
        str(uuid.uuid4()),
        email,
        fn or "",
        ln or "",
        (row.get("business_name") or "").strip() or "",
        (row.get("phone") or "").strip() or "",
        (row.get("address") or "").strip() or "",
        (row.get("city") or "").strip() or "",
        (row.get("state") or "").strip() or "",
        (row.get("country") or "").strip() or "",
        (row.get("segment") or row.get("industry") or "").strip() or "",
        (row.get("website") or "").strip() or "",
        datetime.now(timezone.utc),
    )


def main() -> None:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")

    parser = argparse.ArgumentParser(description="Import contacts CSV into PostgreSQL (contacts table only).")
    parser.add_argument(
        "--csv",
        type=Path,
        default=root / "output" / "sba" / "all_businesses.csv",
        help="Path to all_businesses.csv",
    )
    parser.add_argument("--batch-size", type=int, default=1000, help="Rows per INSERT batch")
    args = parser.parse_args()

    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        raise SystemExit("DATABASE_URL is not set. Add it to .env or export it before running.")

    try:
        import psycopg2  # noqa: PLC0415
        from psycopg2.extras import execute_values  # noqa: PLC0415
    except ImportError as exc:
        raise SystemExit("psycopg2 is required. Install with: pip3 install psycopg2-binary") from exc

    csv_path = args.csv if args.csv.is_absolute() else (root / args.csv).resolve()
    if not csv_path.is_file():
        raise SystemExit(f"CSV not found: {csv_path}")

    insert_sql = """
        INSERT INTO contacts (
            id, email, first_name, last_name, business_name, phone, address,
            city, state, country, industry, website, created_at
        ) VALUES %s
        ON CONFLICT (email) DO NOTHING
    """

    # One INSERT statement cannot repeat the same email twice; dedupe within each batch by email.
    batch_by_email: Dict[str, Tuple[Any, ...]] = {}
    csv_rows = 0
    skipped_invalid = 0
    inserted_total = 0

    def flush_batch(cur: Any) -> None:
        nonlocal inserted_total, batch_by_email
        if not batch_by_email:
            return
        rows = list(batch_by_email.values())
        execute_values(cur, insert_sql, rows, page_size=len(rows))
        rc = cur.rowcount
        inserted_total += int(rc) if rc is not None and rc >= 0 else 0
        batch_by_email.clear()

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            with csv_path.open("r", encoding="utf-8", newline="") as fp:
                reader = csv.DictReader(fp)
                for row in reader:
                    csv_rows += 1
                    tup = row_to_tuple(row)
                    if tup is None:
                        skipped_invalid += 1
                    else:
                        batch_by_email[tup[1]] = tup
                    if len(batch_by_email) >= args.batch_size:
                        flush_batch(cur)
                        conn.commit()
                    if csv_rows % 10_000 == 0:
                        print(
                            f"Progress: {csv_rows:,} CSV rows read; "
                            f"rows inserted so far (ON CONFLICT excluded): {inserted_total:,}"
                        )
            flush_batch(cur)
            conn.commit()
    finally:
        conn.close()

    print(
        f"Done. CSV rows read: {csv_rows:,}; skipped (no email): {skipped_invalid:,}; "
        f"insert attempts committed in batches (rowcount sum): {inserted_total:,}. "
        f"Duplicates were skipped via ON CONFLICT (email) DO NOTHING."
    )


if __name__ == "__main__":
    main()
