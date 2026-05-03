"""
Suppression lists: unsubscribes, bounces, complaints.

CSV files under data/suppression/ with a single column: email
(lowercased). Thread-safe append via file locks.
"""

from __future__ import annotations

import csv
import fcntl
import os
from pathlib import Path
from typing import Iterable, Set

DEFAULT_SUPPRESSION_DIR = Path("data/suppression")


def _path_for(kind: str, base_dir: Path) -> Path:
    """Resolve CSV path for internal kind: unsubscribe | bounce | complaint."""
    mapping = {
        "unsubscribe": base_dir / "unsubscribes.csv",
        "bounce": base_dir / "bounces.csv",
        "complaint": base_dir / "complaints.csv",
    }
    if kind not in mapping:
        raise ValueError(f"Unknown suppression kind: {kind}")
    return mapping[kind]


def _ensure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", encoding="utf-8", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(["email"])


def load_suppression_lists(base_dir: Path | None = None) -> Set[str]:
    """Load all suppressed emails (union of three lists)."""
    base = base_dir or DEFAULT_SUPPRESSION_DIR
    emails: Set[str] = set()
    for name in ("unsubscribes.csv", "bounces.csv", "complaints.csv"):
        p = base / name
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                e = (row.get("email") or "").strip().lower()
                if e:
                    emails.add(e)
    return emails


def is_suppressed(email: str, cache: Set[str] | None = None, base_dir: Path | None = None) -> bool:
    normalized = (email or "").strip().lower()
    if not normalized:
        return True
    if cache is not None:
        return normalized in cache
    return normalized in load_suppression_lists(base_dir)


def add_to_suppression(email: str, event_type: str, base_dir: Path | None = None) -> None:
    """
    Append one email to the appropriate CSV.

    event_type: unsubscribe | bounce | dropped | spamreport | complaint
    """
    base = base_dir or DEFAULT_SUPPRESSION_DIR
    kind = (event_type or "").strip().lower()
    if kind in ("unsubscribe", "unsubscribed"):
        path = _path_for("unsubscribe", base)
    elif kind in ("bounce", "bounced", "dropped"):
        # SendGrid "dropped" often indicates policy/block; treat as bounce list for suppression.
        path = _path_for("bounce", base)
    elif kind in ("spamreport", "complaint", "spam_complaint"):
        path = _path_for("complaint", base)
    else:
        path = _path_for("bounce", base)

    _ensure_file(path)
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized:
        return

    with path.open("a+", encoding="utf-8", newline="") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            fp.seek(0, os.SEEK_END)
            writer = csv.writer(fp)
            writer.writerow([normalized])
            fp.flush()
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def seed_suppression_files(base_dir: Path | None = None) -> None:
    """Create empty CSVs with headers if missing."""
    base = base_dir or DEFAULT_SUPPRESSION_DIR
    for name in ("unsubscribes.csv", "bounces.csv", "complaints.csv", "dormant.csv"):
        p = base / name
        if name == "dormant.csv":
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("w", encoding="utf-8", newline="") as fp:
                    writer = csv.writer(fp)
                    writer.writerow(["email", "dormant_since"])
            continue
        _ensure_file(p)


def load_email_column_csv(path: Path, column: str = "email") -> Set[str]:
    """Load a single-column or named-column email set from CSV."""
    out: Set[str] = set()
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            e = (row.get(column) or row.get("email") or "").strip().lower()
            if e and "@" in e:
                out.add(e)
    return out
