"""Dormant contacts: after Email 4 with no reply — 90-day pause before a new Email 1 cycle."""

from __future__ import annotations

import csv
import fcntl
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

DORMANT_COOLDOWN_DAYS = 90


def dormant_csv_path(base_dir: Path) -> Path:
    return base_dir / "dormant.csv"


def load_dormant_since(base_dir: Path) -> dict[str, date]:
    """email -> dormant_since date (first occurrence wins if duplicates)."""
    path = dormant_csv_path(base_dir)
    out: dict[str, date] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            em = (row.get("email") or "").strip().lower()
            ds = row.get("dormant_since") or ""
            if not em or not ds:
                continue
            try:
                d = date.fromisoformat(str(ds).strip()[:10])
            except ValueError:
                continue
            if em not in out:
                out[em] = d
    return out


def is_dormant_cooldown_active(email: str, base_dir: Path, today: date) -> bool:
    """True if contact is within 90-day dormant window after last Email 4."""
    m = load_dormant_since(base_dir)
    em = email.strip().lower()
    if em not in m:
        return False
    return (today - m[em]).days < DORMANT_COOLDOWN_DAYS


def append_dormant(email: str, dormant_since: date, base_dir: Path) -> None:
    path = dormant_csv_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", encoding="utf-8", newline="") as fp:
            w = csv.writer(fp)
            w.writerow(["email", "dormant_since"])
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized:
        return
    with path.open("a+", encoding="utf-8", newline="") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            fp.seek(0, os.SEEK_END)
            w = csv.writer(fp)
            w.writerow([normalized, dormant_since.isoformat()])
            fp.flush()
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def days_until_dormant_eligible(email: str, base_dir: Path, today: date) -> Optional[int]:
    """Days remaining until 90-day window ends, or None if not dormant."""
    m = load_dormant_since(base_dir)
    em = email.strip().lower()
    if em not in m:
        return None
    elapsed = (today - m[em]).days
    if elapsed >= DORMANT_COOLDOWN_DAYS:
        return 0
    return DORMANT_COOLDOWN_DAYS - elapsed
