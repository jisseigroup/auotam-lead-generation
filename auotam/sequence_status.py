"""
Per-contact sequence state: JSONL file (legacy) or sequence_status table when DATABASE_URL is set.

Tracks Email 1–4 send dates (EST calendar dates), flags, and last send date
for the one-email-per-contact-per-day rule.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SEQUENCE_STATUS_PATH = Path("data/sequence/status.jsonl")


def default_record(email: str) -> Dict[str, Any]:
    em = (email or "").strip().lower()
    return {
        "email": em,
        "email_1_sent": "",
        "email_2_sent": "",
        "email_3_sent": "",
        "email_4_sent": "",
        "replied": False,
        "unsubscribed": False,
        "bounced": False,
        "dormant": False,
        "lead_score": "cold",
        "last_sent_date_est": "",
    }


def load_sequence_state(path: Path | None = None) -> Dict[str, Dict[str, Any]]:
    from auotam import pg_store

    if pg_store.use_database():
        return pg_store.load_sequence_state_dict()
    p = path or DEFAULT_SEQUENCE_STATUS_PATH
    out: Dict[str, Dict[str, Any]] = {}
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            em = (rec.get("email") or "").strip().lower()
            if em:
                out[em] = rec
    return out


def save_sequence_state(path: Path, state: Dict[str, Dict[str, Any]]) -> None:
    from auotam import pg_store

    if pg_store.use_database():
        pg_store.save_sequence_state_dict(state)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(state[k], sort_keys=True) for k in sorted(state.keys())]
    fd, tmp = tempfile.mkstemp(prefix="seq_", suffix=".jsonl", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            for line in lines:
                fp.write(line + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def parse_iso_date(value: str) -> Optional[date]:
    if not value or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def merge_list_flags_into_record(
    rec: Dict[str, Any],
    email: str,
    unsub_set: set,
    bounce_set: set,
) -> None:
    em = email.strip().lower()
    if em in unsub_set:
        rec["unsubscribed"] = True
    if em in bounce_set:
        rec["bounced"] = True
