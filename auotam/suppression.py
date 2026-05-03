"""
Suppression lists: unsubscribes, bounces, complaints.

CSV files under data/suppression/ (legacy), or suppression table when DATABASE_URL is set.
"""

from __future__ import annotations

import csv
import fcntl
import os
from pathlib import Path
from typing import Iterable, Set, Tuple

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
    """Load all suppressed emails (union of unsubscribe / bounce / complaint)."""
    from auotam import pg_store

    if pg_store.use_database():
        return pg_store.load_suppression_email_union()
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


def load_unsub_and_bounce_sets(base_dir: Path | None = None) -> Tuple[Set[str], Set[str]]:
    """
    Match legacy split: unsubscribes.csv -> unsub_set, bounces.csv -> bounce_set
    (complaints are not merged into sequence flags; they remain in suppression union only).
    """
    from auotam import pg_store

    if pg_store.use_database():
        return pg_store.load_unsub_and_bounce_email_sets()
    base = base_dir or DEFAULT_SUPPRESSION_DIR
    unsub = load_email_column_csv(base / "unsubscribes.csv")
    bounce = load_email_column_csv(base / "bounces.csv")
    return unsub, bounce


def is_suppressed(email: str, cache: Set[str] | None = None, base_dir: Path | None = None) -> bool:
    normalized = (email or "").strip().lower()
    if not normalized:
        return True
    if cache is not None:
        return normalized in cache
    return normalized in load_suppression_lists(base_dir)


def add_to_suppression(email: str, event_type: str, base_dir: Path | None = None) -> None:
    """
    Record suppression (CSV legacy or PostgreSQL).

    event_type: unsubscribe | bounce | dropped | spamreport | complaint
    """
    from auotam import pg_store

    if pg_store.use_database():
        kind = (event_type or "").strip().lower()
        if kind in ("unsubscribe", "unsubscribed"):
            reason = "unsubscribe"
        elif kind in ("bounce", "bounced", "dropped"):
            reason = "bounce"
        elif kind in ("spamreport", "complaint", "spam_complaint"):
            reason = "complaint"
        else:
            reason = "bounce"
        pg_store.add_suppression(email, reason)
        return

    base = base_dir or DEFAULT_SUPPRESSION_DIR
    kind = (event_type or "").strip().lower()
    if kind in ("unsubscribe", "unsubscribed"):
        path = _path_for("unsubscribe", base)
    elif kind in ("bounce", "bounced", "dropped"):
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
    """Create empty CSVs with headers if missing (file mode only)."""
    from auotam import pg_store

    if pg_store.use_database():
        return
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
