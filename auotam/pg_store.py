"""
PostgreSQL persistence for AUOTAM lead-gen (when DATABASE_URL is set).

Mirrors former CSV/JSONL behavior: email_log, sequence_status, suppression, contacts, lead_status.
When DATABASE_URL is unset, callers use file-based paths unchanged.
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from zoneinfo import ZoneInfo

from auotam.db import database_url, get_connection

EST = ZoneInfo("America/New_York")

DORMANT_COOLDOWN_DAYS = 90

_UNSUB_REASONS = frozenset(
    {"unsubscribe", "unsubscribed", "complaint", "spamreport", "spam_complaint"}
)
_BOUNCE_REASONS = frozenset({"bounce", "bounced", "dropped"})

_BLOCKED_LEAD_STATUSES = frozenset({"won", "not_interested"})


def use_database() -> bool:
    return bool(database_url())


def _est_day_bounds(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """
    Half-open [start, end) for the current America/New_York calendar day as absolute instants.

    Used for daily-cap / domain counts against `timestamptz` so we never rely on UTC midnight
    or ambiguous `date AT TIME ZONE` interpretations in PostgreSQL.
    """
    dt = (now or datetime.now(tz=EST)).astimezone(EST)
    day = dt.date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=EST)
    end = start + timedelta(days=1)
    return start, end


def _parse_date(val: Any) -> Optional[date]:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.astimezone(EST).date()
    s = str(val).strip()[:10]
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _row_to_iso_date(val: Any) -> str:
    d = _parse_date(val)
    return d.isoformat() if d else ""


def _latest_sequence_sent_iso(*vals: Any) -> str:
    """Latest calendar day among sequence step sends (not CRM last_communication)."""
    dates = [d for v in vals if (d := _parse_date(v))]
    return max(dates).isoformat() if dates else ""


def _split_name(owner_name: str) -> Tuple[str, str]:
    parts = (owner_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _load_unsub_bounce_sets_cur(cur) -> Tuple[Set[str], Set[str]]:
    unsub: Set[str] = set()
    bounce: Set[str] = set()
    cur.execute("SELECT lower(email), lower(coalesce(reason::text, '')) FROM suppression")
    for em, reason in cur.fetchall():
        if not em:
            continue
        r = (reason or "").strip().lower()
        if r in _UNSUB_REASONS:
            unsub.add(em)
        if r in _BOUNCE_REASONS or r in _UNSUB_REASONS:
            bounce.add(em)
    return unsub, bounce


def get_contact_id_by_email_cur(cur, email: str) -> Optional[str]:
    em = (email or "").strip().lower()
    if not em:
        return None
    cur.execute("SELECT id FROM contacts WHERE lower(email) = %s", (em,))
    row = cur.fetchone()
    return str(row[0]) if row else None


def get_or_create_contact_id_for_row(row: Dict[str, Any]) -> str:
    """Resolve contacts.id for an agent/CSV row (opens one transaction)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            return get_or_create_contact_id_cur(cur, row)


def select_contact_website_by_email(email: str) -> str:
    """Return website from contacts if present (for merging into CSV row when using DB)."""
    em = (email or "").strip().lower()
    if not em:
        return ""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(website, '') FROM contacts WHERE lower(email) = %s LIMIT 1",
                (em,),
            )
            r = cur.fetchone()
            return (r[0] or "").strip() if r else ""


def get_or_create_contact_id_cur(cur, row: Dict[str, Any]) -> str:
    email = (row.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("row must include valid email")
    cid = get_contact_id_by_email_cur(cur, email)
    if cid is not None:
        return cid
    fn, ln = _split_name(row.get("owner_name") or row.get("name") or "")
    cur.execute(
        """
        INSERT INTO contacts (
            email, first_name, last_name, business_name, phone, address,
            city, state, country, industry, website, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        RETURNING id
        """,
        (
            email,
            fn or "",
            ln or "",
            (row.get("business_name") or row.get("company") or "").strip() or "",
            (row.get("phone") or "").strip() or "",
            (row.get("address") or "").strip() or "",
            (row.get("city") or "").strip() or "",
            (row.get("state") or "").strip() or "",
            (row.get("country") or "").strip() or "",
            (row.get("segment") or row.get("industry") or "").strip() or "",
            (row.get("website") or "").strip() or "",
        ),
    )
    (new_id,) = cur.fetchone()
    return str(new_id)


def maybe_bootstrap_contacts_from_csv(csv_path: Path) -> None:
    """If contacts table is empty and CSV exists, bulk-import contacts."""
    if not use_database() or not csv_path.exists():
        return
    rows: List[Tuple[Any, ...]] = []
    seen: Set[str] = set()
    with csv_path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            email = (row.get("email") or "").strip().lower()
            if "@" not in email or email in seen:
                continue
            seen.add(email)
            fn, ln = _split_name(row.get("owner_name") or "")
            rows.append(
                (
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
                )
            )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM contacts")
            (n,) = cur.fetchone()
            if n and n > 0:
                return

        chunk = 2000
        with conn.cursor() as cur:
            for i in range(0, len(rows), chunk):
                part = rows[i : i + chunk]
                cur.executemany(
                    """
                    INSERT INTO contacts (
                        email, first_name, last_name, business_name, phone, address,
                        city, state, country, industry, website, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    part,
                )


def is_lead_status_blocked(contact_id: str) -> bool:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lower(coalesce(status::text, '')) FROM lead_status WHERE contact_id = %s",
                (contact_id,),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return False
            return str(row[0]).strip().lower() in _BLOCKED_LEAD_STATUSES


def load_suppression_email_union() -> Set[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT lower(email) FROM suppression")
            return {r[0] for r in cur.fetchall() if r and r[0]}


def load_unsub_and_bounce_email_sets() -> Tuple[Set[str], Set[str]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            return _load_unsub_bounce_sets_cur(cur)


def add_suppression(email: str, reason: str) -> None:
    em = (email or "").strip().lower()
    if not em or "@" not in em:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO suppression (email, reason, suppressed_at)
                SELECT %s, %s, NOW()
                WHERE NOT EXISTS (SELECT 1 FROM suppression WHERE lower(email) = %s)
                """,
                (em, (reason or "unknown").lower()[:200], em),
            )


def load_sequence_state_dict() -> Dict[str, Dict[str, Any]]:
    """email (lower) -> record compatible with sequence_status.default_record shape."""
    out: Dict[str, Dict[str, Any]] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            unsub, bounce = _load_unsub_bounce_sets_cur(cur)
            cur.execute(
                "SELECT DISTINCT contact_id FROM email_log WHERE replied_at IS NOT NULL"
            )
            replied_ids: Set[str] = {str(r[0]) for r in cur.fetchall() if r and r[0] is not None}
            cur.execute(
                "SELECT contact_id FROM lead_status WHERE lower(coalesce(status::text, '')) = 'replied'"
            )
            replied_ids |= {str(r[0]) for r in cur.fetchall() if r and r[0] is not None}
            cur.execute(
                """
                SELECT
                    lower(c.email) AS email,
                    s.email_1_sent,
                    s.email_2_sent,
                    s.email_3_sent,
                    s.email_4_sent,
                    s.sequence_complete,
                    s.dormant,
                    s.dormant_until,
                    c.id AS contact_id
                FROM sequence_status s
                INNER JOIN contacts c ON c.id = s.contact_id
                """
            )
            for row in cur.fetchall():
                (
                    em,
                    e1,
                    e2,
                    e3,
                    e4,
                    seq_complete,
                    dormant,
                    dormant_until,
                    cid,
                ) = row
                em = (em or "").strip().lower()
                if not em:
                    continue
                replied = str(cid) in replied_ids
                out[em] = {
                    "email": em,
                    "email_1_sent": _row_to_iso_date(e1),
                    "email_2_sent": _row_to_iso_date(e2),
                    "email_3_sent": _row_to_iso_date(e3),
                    "email_4_sent": _row_to_iso_date(e4),
                    "replied": replied,
                    "unsubscribed": em in unsub,
                    "bounced": em in bounce,
                    "dormant": bool(dormant),
                    "lead_score": "cold",
                    "last_sent_date_est": _latest_sequence_sent_iso(e1, e2, e3, e4),
                    "sequence_complete": bool(seq_complete),
                    "dormant_until": _row_to_iso_date(dormant_until),
                }
    return out


def _sequence_tuple_from_record(rec: Dict[str, Any]) -> Tuple[Any, ...]:
    e1 = _parse_date(rec.get("email_1_sent"))
    e2 = _parse_date(rec.get("email_2_sent"))
    e3 = _parse_date(rec.get("email_3_sent"))
    e4 = _parse_date(rec.get("email_4_sent"))
    last_comm = _parse_date(rec.get("last_sent_date_est"))
    seq_complete = bool(rec.get("email_4_sent"))
    dormant = bool(rec.get("dormant"))
    dormant_until = _parse_date(rec.get("dormant_until"))
    return (e1, e2, e3, e4, last_comm, seq_complete, dormant, dormant_until)


def save_sequence_state_dict(state: Dict[str, Dict[str, Any]]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for em, rec in state.items():
                em = (em or rec.get("email") or "").strip().lower()
                if not em:
                    continue
                cid = get_contact_id_by_email_cur(cur, em)
                if cid is None:
                    cid = get_or_create_contact_id_cur(cur, {"email": em})
                e1, e2, e3, e4, last_comm, seq_complete, dormant, dormant_until = (
                    _sequence_tuple_from_record(rec)
                )
                cur.execute(
                    """
                    UPDATE sequence_status SET
                        email_1_sent = %s,
                        email_2_sent = %s,
                        email_3_sent = %s,
                        email_4_sent = %s,
                        last_communication = %s,
                        sequence_complete = %s,
                        dormant = %s,
                        dormant_until = %s
                    WHERE contact_id = %s
                    """,
                    (
                        e1,
                        e2,
                        e3,
                        e4,
                        last_comm,
                        seq_complete,
                        dormant,
                        dormant_until,
                        cid,
                    ),
                )
                if cur.rowcount == 0:
                    cur.execute(
                        """
                        INSERT INTO sequence_status (
                            contact_id, email_1_sent, email_2_sent, email_3_sent, email_4_sent,
                            last_communication, next_scheduled_email, sequence_complete, dormant, dormant_until
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s)
                        """,
                        (
                            cid,
                            e1,
                            e2,
                            e3,
                            e4,
                            last_comm,
                            seq_complete,
                            dormant,
                            dormant_until,
                        ),
                    )


def is_dormant_cooldown_active_db(email: str, today: date) -> bool:
    em = (email or "").strip().lower()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.dormant, s.dormant_until
                FROM sequence_status s
                JOIN contacts c ON c.id = s.contact_id
                WHERE lower(c.email) = %s
                """,
                (em,),
            )
            row = cur.fetchone()
            if not row:
                return False
            dormant, du = row[0], row[1]
            if not dormant or not du:
                return False
            du_d = _parse_date(du)
            if not du_d:
                return False
            return today < du_d


def load_dormant_since_map() -> Dict[str, date]:
    """email -> dormant_since (derived from dormant_until − 90 days)."""
    out: Dict[str, date] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT lower(c.email), s.dormant_until
                FROM sequence_status s
                JOIN contacts c ON c.id = s.contact_id
                WHERE s.dormant IS TRUE AND s.dormant_until IS NOT NULL
                """
            )
            for em, du in cur.fetchall():
                if not em or not du:
                    continue
                end = _parse_date(du)
                if not end:
                    continue
                since = end - timedelta(days=DORMANT_COOLDOWN_DAYS)
                if em not in out:
                    out[em] = since
    return out


def append_dormant_db(email: str, dormant_since: date) -> None:
    em = (email or "").strip().lower()
    until = dormant_since + timedelta(days=DORMANT_COOLDOWN_DAYS)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cid = get_contact_id_by_email_cur(cur, em)
            if cid is None:
                cid = get_or_create_contact_id_cur(cur, {"email": em})
            cur.execute(
                """
                UPDATE sequence_status SET
                    dormant = TRUE,
                    dormant_until = %s,
                    sequence_complete = TRUE
                WHERE contact_id = %s
                """,
                (until, cid),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO sequence_status (
                        contact_id, email_1_sent, email_2_sent, email_3_sent, email_4_sent,
                        last_communication, next_scheduled_email, sequence_complete, dormant, dormant_until
                    )
                    VALUES (%s, NULL, NULL, NULL, NULL, NULL, NULL, TRUE, TRUE, %s)
                    """,
                    (cid, until),
                )


def sent_today_count_est() -> int:
    """Count successful lead-gen sequence sends logged today (EST), from email_log only."""
    start, end = _est_day_bounds()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM email_log
                WHERE direction = 'outbound'
                  AND status = 'sent'
                  AND email_type IN ('initial', 'followup_2', 'followup_3', 'followup_4')
                  AND sent_at >= %s AND sent_at < %s
                """,
                (start, end),
            )
            (n,) = cur.fetchone()
            return int(n or 0)


def recipient_sent_today_est(email: str) -> bool:
    """
    True if this contact already has a successful initial send logged for the current EST calendar day.

    Mirrors: SELECT count(*) FROM email_log WHERE contact_id = ... AND sent_at is today (EST)
    AND email_type = 'initial' — if count > 0, skip another initial today.
    """
    em = (email or "").strip().lower()
    if not em:
        return False
    start, end = _est_day_bounds()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cid = get_contact_id_by_email_cur(cur, em)
            if cid is None:
                return False
            cur.execute(
                """
                SELECT COUNT(*) FROM email_log
                WHERE contact_id = %s
                  AND email_type = 'initial'
                  AND direction = 'outbound'
                  AND status = 'sent'
                  AND sent_at >= %s AND sent_at < %s
                """,
                (cid, start, end),
            )
            (n,) = cur.fetchone()
            return int(n or 0) > 0


def emails_with_initial_sent_today_est() -> Set[str]:
    """
    Lowercased recipient emails that already have a successful *initial* send today (EST),
    matching recipient_sent_today_est. Used to avoid per-contact DB round-trips in orchestrate.
    """
    start, end = _est_day_bounds()
    out: Set[str] = set()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT lower(c.email)
                FROM email_log el
                JOIN contacts c ON c.id = el.contact_id
                WHERE el.email_type = 'initial'
                  AND el.direction = 'outbound'
                  AND el.status = 'sent'
                  AND el.sent_at >= %s AND el.sent_at < %s
                """,
                (start, end),
            )
            for (em,) in cur.fetchall():
                if em:
                    out.add(str(em).strip().lower())
    return out


def domain_sent_today_est() -> Dict[str, int]:
    start, end = _est_day_bounds()
    counts: Dict[str, int] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT split_part(lower(c.email), '@', 2) AS dom, COUNT(*)
                FROM email_log el
                JOIN contacts c ON c.id = el.contact_id
                WHERE el.direction = 'outbound'
                  AND el.status = 'sent'
                  AND el.email_type IN ('initial', 'followup_2', 'followup_3', 'followup_4')
                  AND el.sent_at >= %s AND el.sent_at < %s
                GROUP BY 1
                """,
                (start, end),
            )
            for dom, n in cur.fetchall():
                if dom:
                    counts[str(dom)] = int(n)
    return counts


def _sent_at_from_agent_row(row: Dict[str, Any]) -> datetime:
    ts = row.get("timestamp_est") or datetime.now(tz=EST).isoformat()
    try:
        sent_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        sent_at = datetime.now(tz=EST)
    if sent_at.tzinfo is None:
        return sent_at.replace(tzinfo=EST)
    return sent_at.astimezone(EST)


def insert_email_log_from_agent_rows(rows: List[Dict[str, Any]]) -> None:
    """Append many send-log rows in one DB transaction (one connection)."""
    if not rows:
        return
    valid_statuses = {
        "sent",
        "bounced",
        "opened",
        "clicked",
        "replied",
        "skipped",
        "failed",
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            for row in rows:
                email = (row.get("email") or "").strip().lower()
                if not email:
                    continue
                sent_at = _sent_at_from_agent_row(row)
                agent_row = {
                    "email": email,
                    "owner_name": row.get("name") or "",
                    "business_name": row.get("company") or "",
                    "segment": row.get("segment") or "",
                }
                cid = get_or_create_contact_id_cur(cur, agent_row)
                status = (row.get("status") or "").strip().lower() or "unknown"
                if status not in valid_statuses:
                    status = "skipped"
                mid = (row.get("message_id") or "").strip() or None
                log_id = str(uuid.uuid4())
                body = (row.get("body") or row.get("body_text") or "").strip() or None
                cur.execute(
                    """
                    INSERT INTO email_log (
                        id, contact_id, email_type, subject, body, template_variant, sent_at,
                        message_id, status, opened_at, clicked_at, replied_at, direction
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, 'outbound')
                    """,
                    (
                        log_id,
                        cid,
                        (row.get("mail_kind") or "unknown")[:64],
                        (row.get("subject") or "")[:2000],
                        body,
                        (row.get("template_variant") or "")[:32],
                        sent_at,
                        mid,
                        status[:64],
                    ),
                )


def insert_email_log_from_agent_row(row: Dict[str, Any]) -> None:
    """Append one logical send-log row (matches legacy CSV columns)."""
    insert_email_log_from_agent_rows([row])


def ingest_ses_events_from_jsonl(sns_path: Path) -> int:
    """Apply SES/SNS event lines to email_log (replaces sidecar JSONL merge)."""
    if not sns_path.exists():
        return 0
    with sns_path.open("r", encoding="utf-8") as fp:
        lines = fp.readlines()
    touched = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "Message" in evt and isinstance(evt["Message"], str):
                    try:
                        evt = json.loads(evt["Message"])
                    except json.JSONDecodeError:
                        pass
                message_id = (evt.get("mail") or {}).get("messageId")
                if not message_id:
                    continue
                event_type = (evt.get("eventType") or "").lower()
                now = datetime.now(tz=EST)
                n = 0
                if event_type == "bounce":
                    cur.execute(
                        "UPDATE email_log SET status = 'bounced' WHERE message_id = %s",
                        (message_id,),
                    )
                    n = cur.rowcount
                    cur.execute(
                        """
                        SELECT c.email FROM email_log el
                        JOIN contacts c ON c.id = el.contact_id
                        WHERE el.message_id = %s
                        LIMIT 1
                        """,
                        (message_id,),
                    )
                    erow = cur.fetchone()
                    if erow and erow[0]:
                        cur.execute(
                            """
                            INSERT INTO suppression (email, reason, suppressed_at)
                            SELECT lower(%s), 'bounce', NOW()
                            WHERE NOT EXISTS (SELECT 1 FROM suppression WHERE lower(email) = lower(%s))
                            """,
                            (erow[0], erow[0]),
                        )
                elif event_type == "open":
                    cur.execute(
                        """
                        UPDATE email_log SET opened_at = COALESCE(opened_at, %s)
                        WHERE message_id = %s
                        """,
                        (now, message_id),
                    )
                    n = cur.rowcount
                elif event_type == "click":
                    cur.execute(
                        """
                        UPDATE email_log SET clicked_at = COALESCE(clicked_at, %s)
                        WHERE message_id = %s
                        """,
                        (now, message_id),
                    )
                    n = cur.rowcount
                elif event_type in {"delivery", "send"}:
                    # Keep status='sent' so daily-cap / domain counts match legacy CSV behavior.
                    n = 0
                elif event_type == "complaint":
                    cur.execute(
                        "UPDATE email_log SET status = 'complaint' WHERE message_id = %s",
                        (message_id,),
                    )
                    n = cur.rowcount
                    cur.execute(
                        """
                        SELECT c.email FROM email_log el
                        JOIN contacts c ON c.id = el.contact_id
                        WHERE el.message_id = %s LIMIT 1
                        """,
                        (message_id,),
                    )
                    erow = cur.fetchone()
                    if erow and erow[0]:
                        cur.execute(
                            """
                            INSERT INTO suppression (email, reason, suppressed_at)
                            SELECT lower(%s), 'complaint', NOW()
                            WHERE NOT EXISTS (SELECT 1 FROM suppression WHERE lower(email) = lower(%s))
                            """,
                            (erow[0], erow[0]),
                        )
                elif event_type == "reject":
                    cur.execute(
                        "UPDATE email_log SET status = 'rejected' WHERE message_id = %s",
                        (message_id,),
                    )
                    n = cur.rowcount
                if n:
                    touched += n
    return touched
