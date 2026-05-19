#!/usr/bin/env python3
"""
AUOTAM outbound email sending agent.

Supports:
- SendGrid (preferred when SENDGRID_API_KEY is set)
- Amazon SES (fallback when SENDGRID_API_KEY is not set)

Features:
- Mon-Fri only
- US business-hour gate (America/New_York)
- Daily cap + per-sending-domain daily cap
- Suppression lists (unsubscribe / bounce / complaint)
- Segment template variants (A/B/C) with random rotation
- Mail merge + unsubscribe footer link
- Pre-send validation (role addresses, invalid, duplicates)
- Rate limiting
- Send log + optional status JSONL
- First successful send of each EST day: optional mirror to govind@auotam.com with [TEST COPY] subject (see DAILY_TEST_COPY_*; tracked beside --log-csv).

Usage examples:
  python3 email_agent.py send --input-csv output/sba/all_businesses.csv
  python3 email_agent.py ingest-events --sns-jsonl ses_events.jsonl
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import formataddr
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from auotam.sendgrid_client import send_mail as sendgrid_send_mail
from auotam.identities import pick_identity
from auotam.dormant_list import DORMANT_COOLDOWN_DAYS, append_dormant, is_dormant_cooldown_active, load_dormant_since
from auotam.followup_templates import build_followup
from auotam.sequence_status import (
    DEFAULT_SEQUENCE_STATUS_PATH,
    default_record,
    load_sequence_state,
    merge_list_flags_into_record,
    parse_iso_date,
    save_sequence_record,
    save_sequence_state,
)
from auotam.suppression import (
    DEFAULT_SUPPRESSION_DIR,
    is_suppressed,
    load_suppression_lists,
    load_unsub_and_bounce_sets,
    seed_suppression_files,
)

EST = ZoneInfo("America/New_York")

ROLE_LOCALPART_PREFIXES = ("info", "support", "admin", "sales", "help", "contact", "office")


def merge_contact_website_from_db(row: dict, email_lower: str) -> dict:
    """When using PostgreSQL, prefer `website` stored on contacts over CSV if set."""
    from auotam import pg_store

    if not email_lower or not pg_store.use_database():
        return row
    dbw = pg_store.select_contact_website_by_email(email_lower)
    if not dbw:
        return row
    return {**row, "website": dbw}

LOG_FIELDS = [
    "timestamp_est",
    "sent_date_est",
    "status",
    "reason",
    "email",
    "name",
    "company",
    "segment",
    "mail_kind",
    "template_variant",
    "sending_domain",
    "subject",
    "body",
    "message_id",
    "entity_detail_id",
    "uei",
]


def _tpl(subject: str, body: str) -> Dict[str, str]:
    return {"subject": subject, "body": body}


COMMON_CLOSE = (
    "Book a free 30-min call: https://auotam.com/book\n\n"
    "Govind Chauhan\n"
    "Founder, AUOTAM"
)
CASE_STUDIES_CLOSE = (
    "See how we've done this across industries: https://auotam.com/case-studies\n\n"
    "Book a free 30-min call: https://auotam.com/book\n\n"
    "Govind Chauhan\n"
    "Founder, AUOTAM"
)

DEFAULT_A_SUBJECT = "How much time is your team losing to manual work?"
DEFAULT_A_BODY = (
    "Hi {first_name},\n\n"
    "Most businesses we talk to are running on 5+ disconnected tools, no central system, "
    "and staff doing manually what should be automated.\n\n"
    "The result - hours lost every week, inconsistent operations, and growth that's harder "
    "than it needs to be.\n\n"
    "We build custom AI systems, automations, and apps that tie everything together - so your "
    "team stops managing tools and starts getting results.\n\n"
    "We've done this for housing authorities, eCommerce brands, nonprofits, and government "
    "programs across the US.\n\n"
    f"{COMMON_CLOSE}"
)

TEMPLATE_VARIANTS = {
    "ecommerce": {
        "A": _tpl(
            "$2M in sales - here's the system behind it",
            "Hi {first_name},\n\n"
            "One of our eCommerce clients hit $2,014,382 in sales across 7,562 orders - "
            "without growing their team.\n\n"
            "They were drowning in manual inventory, fulfillment bottlenecks, and payment "
            "issues every time traffic spiked.\n\n"
            "We automated the entire order lifecycle. Revenue scaled. Team didn't burn out.\n\n"
            "Every month without this, you're leaving money on the table.\n\n"
            f"{COMMON_CLOSE}",
        ),
        "B": _tpl(
            "7,562 orders. Zero extra headcount. Here's how.",
            "Hi {first_name},\n\n"
            "A retail client of ours processed 7,562 orders in one period without hiring a "
            "single extra person.\n\n"
            "The secret wasn't hustle - it was removing every manual step from their order "
            "lifecycle.\n\n"
            "Inventory, routing, payments - all automated. The team stopped firefighting and "
            "started scaling.\n\n"
            "Every day you're running manual operations, a competitor who automated is pulling "
            "ahead.\n\n"
            f"{COMMON_CLOSE}",
        ),
        "C": _tpl(
            "Your ops team is doing work a system should be doing",
            "Hi {first_name},\n\n"
            "Most eCommerce businesses we work with have the same problem - their team is smart, "
            "but they're spending half their day on tasks that should be automated.\n\n"
            "Inventory updates. Payment exceptions. Fulfillment routing. All manual. All "
            "expensive.\n\n"
            "We rebuilt one client's entire backend - they hit $2M+ in sales the same period "
            "without adding headcount.\n\n"
            "That margin difference is sitting in your operations right now.\n\n"
            f"{COMMON_CLOSE}",
        ),
    },
    "nonprofit": {
        "A": _tpl(
            "$10,000/month in free ad money - is your nonprofit getting it?",
            "Hi {first_name},\n\n"
            "We helped an autism nonprofit unlock $10,000/month in Google Ad Grants - generating "
            "100,000+ impressions and 6,000+ clicks.\n\n"
            "Most nonprofits qualify for this free advertising but never get approved or set it "
            "up correctly.\n\n"
            "We handled the entire process - platform, payment gateway, grant approval, and "
            "advertising execution - as one connected system.\n\n"
            "Every month without this, your mission is invisible to people actively searching for "
            "it.\n\n"
            f"{COMMON_CLOSE}",
        ),
        "B": _tpl(
            "Your nonprofit is leaving $10,000/month on the table",
            "Hi {first_name},\n\n"
            "Google gives nonprofits $10,000 every month in free advertising. Most never claim it.\n\n"
            "Either they don't know about it, or they apply and get rejected because their digital "
            "foundation isn't ready.\n\n"
            "We helped an autism nonprofit get approved, set up their platform, and generate "
            "100,000+ impressions - all through the grant.\n\n"
            "Your mission deserves to be found by the people searching for it.\n\n"
            f"{COMMON_CLOSE}",
        ),
        "C": _tpl(
            "100,000 people could be finding your nonprofit right now",
            "Hi {first_name},\n\n"
            "Most nonprofits rely on word of mouth and donor networks. But the people who need "
            "your services most are searching online - and not finding you.\n\n"
            "We helped one nonprofit fix that. $10,000/month in Google Ad Grants. 100,000+ "
            "impressions. 6,000+ clicks.\n\n"
            "The grant exists. The audience is searching. The only missing piece is the system "
            "to connect them.\n\n"
            f"{COMMON_CLOSE}",
        ),
    },
    "housing_real_estate": {
        "A": _tpl(
            "Processing 20,000 property applications in 4 seconds",
            "Hi {first_name},\n\n"
            "We helped a New Jersey affordable housing program cut application processing from 15 "
            "minutes to 4 seconds - across 20,000+ applications.\n\n"
            "Staff were drowning in incomplete packets, parallel email threads, and manual "
            "exception handling every time policy shifted.\n\n"
            "We built an AI-assisted intake system that validates, routes, and tracks every "
            "application automatically - with full audit trail.\n\n"
            "Every day without this, your staff is doing work a system should be doing - and "
            "applicants are waiting longer than they should.\n\n"
            f"{COMMON_CLOSE}",
        ),
        "B": _tpl(
            "20,000 applications. 4 seconds each. No extra staff.",
            "Hi {first_name},\n\n"
            "A housing program in New Jersey was spending 15 minutes reviewing every single "
            "application manually.\n\n"
            "We cut that to 4 seconds - across 20,000+ applications - without adding a single "
            "staff member.\n\n"
            "AI-assisted validation, automated routing, full audit trail. The team stopped "
            "chasing paperwork and started making decisions.\n\n"
            "If your team is still processing manually, the bottleneck is the system - not the "
            "people.\n\n"
            f"{COMMON_CLOSE}",
        ),
        "C": _tpl(
            "Your staff is reviewing applications that a system should handle",
            "Hi {first_name},\n\n"
            "Every incomplete application your team manually chases is time stolen from the work "
            "that actually matters.\n\n"
            "We built an intake system for a New Jersey housing program that validates, routes, "
            "and tracks 20,000+ applications automatically - with human review only where it "
            "counts.\n\n"
            "Processing time dropped from 15 minutes to 4 seconds per application.\n\n"
            "The same system works for any high-volume intake process - housing, real estate, or "
            "otherwise.\n\n"
            f"{COMMON_CLOSE}",
        ),
    },
    "healthcare": {
        "A": _tpl(DEFAULT_A_SUBJECT, DEFAULT_A_BODY),
        "B": _tpl(
            "Your clinical staff shouldn't be doing admin work",
            "Hi {first_name},\n\n"
            "Every hour a healthcare professional spends on intake forms, follow-up tracking, and "
            "manual reporting is an hour not spent on patients.\n\n"
            "That's not a staffing problem. That's a systems problem.\n\n"
            "We build platforms and automation tools that handle the admin layer - so your "
            "clinical team does what they were trained to do.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
        "C": _tpl(
            "Patients are falling through the cracks - here's why",
            "Hi {first_name},\n\n"
            "In most healthcare practices we talk to, follow-ups get missed not because staff "
            "don't care - but because the system doesn't support them.\n\n"
            "Disconnected tools. Manual tracking. No single view of the patient journey.\n\n"
            "We build connected platforms and automation systems that close those gaps - so no "
            "patient falls through and no staff member burns out chasing paperwork.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
    },
    "construction": {
        "A": _tpl(DEFAULT_A_SUBJECT, DEFAULT_A_BODY),
        "B": _tpl(
            "Every delayed job has a paper trail problem behind it",
            "Hi {first_name},\n\n"
            "Most construction delays we've seen don't start on the job site - they start in the "
            "back office.\n\n"
            "Miscommunication between field and office. Subcontractor scheduling done over text. "
            "Progress updates that nobody sees in time.\n\n"
            "We build systems that connect your field operations, scheduling, and reporting in "
            "one place - so delays get caught before they become problems.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
        "C": _tpl(
            "Your crew is productive. Your systems aren't.",
            "Hi {first_name},\n\n"
            "The best construction teams we've worked with have the same frustration - skilled "
            "people held back by outdated coordination systems.\n\n"
            "Phone calls to schedule. Spreadsheets to track. Emails to report. All manual. All "
            "slow.\n\n"
            "We build custom apps and automation systems that modernize your operations - so your "
            "crew spends time building, not coordinating.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
    },
    "finance": {
        "A": _tpl(DEFAULT_A_SUBJECT, DEFAULT_A_BODY),
        "B": _tpl(
            "Your team is spending billable hours on data entry",
            "Hi {first_name},\n\n"
            "Most financial teams we talk to are doing work that should be automated - pulling "
            "reports, updating records, chasing client follow-ups manually.\n\n"
            "That's not just inefficient. It's expensive.\n\n"
            "We build automation systems that handle your data workflows end to end - so your "
            "team spends time on decisions that actually generate revenue.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
        "C": _tpl(
            "The most expensive person in your firm shouldn't be doing this",
            "Hi {first_name},\n\n"
            "When your highest-paid people spend their day on manual reporting, data entry, and "
            "follow-up tracking - that's a systems problem disguised as a workload problem.\n\n"
            "We build custom platforms and automation workflows that take that work off their "
            "plate entirely.\n\n"
            "The result - your team does what they're best at, and your operations run cleaner.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
    },
    "education": {
        "A": _tpl(DEFAULT_A_SUBJECT, DEFAULT_A_BODY),
        "B": _tpl(
            "Your staff is buried in admin. Students are paying for it.",
            "Hi {first_name},\n\n"
            "In most education organizations we work with, staff spend more time on paperwork "
            "than on the people they're there to serve.\n\n"
            "Enrollment forms. Progress tracking. Communication follow-ups. All manual. All "
            "time-consuming.\n\n"
            "We build platforms and automation systems that handle the administrative layer - so "
            "your staff focuses on students, not spreadsheets.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
        "C": _tpl(
            "Enrollment shouldn't be this hard",
            "Hi {first_name},\n\n"
            "Most education organizations lose prospective students not because of their program "
            "- but because their enrollment and onboarding process is slow, manual, and "
            "frustrating.\n\n"
            "We build custom intake systems and platforms that make enrollment seamless - from "
            "first inquiry to first day - with automated follow-ups at every step.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
    },
    "landscape": {
        "A": _tpl(DEFAULT_A_SUBJECT, DEFAULT_A_BODY),
        "B": _tpl(
            "Scheduling 20 crews manually is a full-time job - it shouldn't be",
            "Hi {first_name},\n\n"
            "Most landscaping businesses we talk to have the same bottleneck - the owner or "
            "office manager spending hours every day just keeping jobs organized.\n\n"
            "Scheduling. Crew assignments. Client updates. Invoicing. All done manually, all "
            "eating time that should go to growing the business.\n\n"
            "We build systems that automate your entire operations layer - so you run more jobs "
            "with less chaos.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
        "C": _tpl(
            "Your landscaping business is growing. Your systems aren't keeping up.",
            "Hi {first_name},\n\n"
            "Growth in landscaping creates a specific problem - more clients, more crews, more "
            "jobs, but the same manual systems trying to hold it all together.\n\n"
            "That's where things break. Double bookings. Missed follow-ups. Invoices that go out "
            "late.\n\n"
            "We build custom apps and automation systems designed for field service businesses - "
            "so your operations scale as fast as your revenue.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
    },
    "government_defense": {
        "A": _tpl(DEFAULT_A_SUBJECT, DEFAULT_A_BODY),
        "B": _tpl(
            "Manual compliance processes are a liability",
            "Hi {first_name},\n\n"
            "In government and defense operations, manual workflows don't just slow things down - "
            "they create audit risk, compliance gaps, and accountability problems.\n\n"
            "Every step that isn't logged, tracked, and attributable is a liability.\n\n"
            "We build secure automation systems with full audit trails - designed for high-stakes, "
            "regulated environments where every action needs to be defensible.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
        "C": _tpl(
            "Your team is spending mission-critical time on administrative work",
            "Hi {first_name},\n\n"
            "The organizations we work with in government and defense have the same frustration - "
            "highly skilled people doing work that systems should be doing.\n\n"
            "Reporting. Compliance tracking. Request processing. All manual. All pulling attention "
            "from what matters.\n\n"
            "We build AI-assisted workflows and secure platforms that handle the administrative "
            "layer - so your team focuses on mission-critical work.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
    },
    "technology": {
        "A": _tpl(DEFAULT_A_SUBJECT, DEFAULT_A_BODY),
        "B": _tpl(
            "Your engineers are building internal tools instead of your product",
            "Hi {first_name},\n\n"
            "Every tech company reaches the same inflection point - the internal tools, manual "
            "processes, and operational gaps start consuming engineering bandwidth that should go "
            "to the core product.\n\n"
            "That's expensive. And it compounds.\n\n"
            "We build custom AI agents and automation systems that handle your internal operations "
            "- so your engineers ship product, not internal tooling.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
        "C": _tpl(
            "The fastest tech teams automate their operations first",
            "Hi {first_name},\n\n"
            "The technology companies that scale fastest aren't just building great products - "
            "they're running lean, automated operations behind the scenes.\n\n"
            "No manual reporting. No disconnected tools. No ops work eating engineering time.\n\n"
            "We build the internal automation layer that lets your team move faster - AI agents, "
            "workflow automation, and custom systems designed for technology companies.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
    },
    "universal": {
        "A": _tpl(DEFAULT_A_SUBJECT, DEFAULT_A_BODY),
        "B": _tpl(
            "5 tools. No system. Sound familiar?",
            "Hi {first_name},\n\n"
            "Most businesses we talk to aren't lacking tools - they're lacking a system that "
            "connects them.\n\n"
            "CRM here. Spreadsheet there. Email for everything else. And someone manually moving "
            "data between all of it.\n\n"
            "We build custom AI systems and automation workflows that replace that patchwork - one "
            "connected operation that runs without manual intervention.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
        "C": _tpl(
            "The hidden cost of manual operations",
            "Hi {first_name},\n\n"
            "Most businesses underestimate how much manual operations are actually costing them - "
            "not just in time, but in errors, delays, and missed opportunities.\n\n"
            "It doesn't show up on a P&L. But it's there every day.\n\n"
            "We build custom automation systems, AI agents, and apps that remove that hidden cost "
            "entirely - so your team operates faster with the same headcount.\n\n"
            f"{CASE_STUDIES_CLOSE}",
        ),
    },
}


def encode_email_token(email: str) -> str:
    """URL-safe base64 (no padding) for unsubscribe links."""
    normalized = (email or "").strip().lower().encode("utf-8")
    token = base64.urlsafe_b64encode(normalized).decode("ascii").rstrip("=")
    return token


def unsubscribe_footer(base_url: str, email: str) -> str:
    base = (base_url or "").rstrip("/")
    token = encode_email_token(email)
    url = f"{base}/unsubscribe?e={token}"
    return (
        "\n\n"
        "---\n\n"
        "If this isn't relevant, you can unsubscribe here:\n\n"
        f"{url}\n"
    )


def sending_domain_from_from_email(from_email: str) -> str:
    return (from_email or "").split("@", 1)[-1].strip().lower()


_PRODUCTION_FROM_EMAIL = "sales@auotam.net"


def _normalize_production_email(email: str) -> str:
    """Map deprecated sales@auotam.com to verified sales@auotam.net."""
    em = (email or "").strip().lower()
    if em == "sales@auotam.com":
        return _PRODUCTION_FROM_EMAIL
    return (email or "").strip()


def resolve_from_email_from_env() -> str:
    """Canonical sender: CLI/env AUOTAM_FROM_EMAIL, then FROM_EMAIL (legacy)."""
    raw = (
        (os.getenv("AUOTAM_FROM_EMAIL") or "").strip()
        or (os.getenv("FROM_EMAIL") or "").strip()
    )
    return _normalize_production_email(raw) or _PRODUCTION_FROM_EMAIL


def _is_ses_unverified_identity_error(reason: str) -> bool:
    r = (reason or "").lower()
    return "not verified" in r or ("messagerejected" in r and "identities failed" in r)


def is_role_address(email: str) -> bool:
    local = (email or "").strip().lower().split("@", 1)[0]
    return any(local.startswith(prefix) for prefix in ROLE_LOCALPART_PREFIXES)


def load_domain_sent_today(log_path: Path) -> Dict[str, int]:
    """Count successful sends today per recipient domain (from log email column)."""
    from auotam import pg_store
    from auotam.db import database_url

    if database_url():
        return pg_store.domain_sent_today_est()
    counts: Dict[str, int] = {}
    if not log_path.exists():
        return counts
    today = datetime.now(tz=EST).date().isoformat()
    with log_path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            if row.get("sent_date_est") != today or row.get("status") != "sent":
                continue
            em = (row.get("email") or row.get("to_email") or "").strip().lower()
            if "@" not in em:
                continue
            dom = em.split("@", 1)[1]
            counts[dom] = counts.get(dom, 0) + 1
    return counts


def is_valid_email(value: str) -> bool:
    if not value:
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()))


def split_first_name(owner_name: str) -> str:
    cleaned = (owner_name or "").strip()
    if not cleaned:
        return "there"
    return cleaned.split()[0].title()


def in_sending_window(start_hour: int, end_hour: int, now: Optional[datetime] = None) -> bool:
    now_est = (now or datetime.now(tz=EST)).astimezone(EST)
    # Monday=0 .. Sunday=6
    if now_est.weekday() >= 5:
        return False
    return start_hour <= now_est.hour < end_hour


@dataclass
class Config:
    provider: str  # sendgrid | ses
    sendgrid_api_key: str
    aws_region: str
    from_email: str
    from_name: str
    configuration_set: Optional[str]
    base_url: str
    suppression_dir: Path
    max_per_day_per_domain: int
    start_hour_est: int
    end_hour_est: int
    daily_cap: int
    sends_per_second: float
    reply_to: Optional[str]


def load_config(args: argparse.Namespace) -> Config:
    sendgrid_key = (args.sendgrid_api_key or os.getenv("SENDGRID_API_KEY", "")).strip()
    provider = (args.provider or os.getenv("EMAIL_PROVIDER", "")).strip().lower()
    # Default: Amazon SES. SendGrid is dormant unless EMAIL_PROVIDER=sendgrid (and key present).
    if not provider:
        provider = "ses"
    if provider == "sendgrid" and not sendgrid_key:
        raise SystemExit(
            "EMAIL_PROVIDER is sendgrid but SENDGRID_API_KEY is missing. "
            "Use SES (default) or set SENDGRID_API_KEY explicitly."
        )

    return Config(
        provider=provider,
        sendgrid_api_key=sendgrid_key,
        aws_region=args.aws_region or os.getenv("AWS_REGION", "us-east-1"),
        from_email=_normalize_production_email(args.from_email) or resolve_from_email_from_env(),
        from_name=args.from_name or os.getenv("AUOTAM_FROM_NAME", "Govind Chauhan"),
        configuration_set=args.configuration_set or os.getenv("SES_CONFIGURATION_SET"),
        base_url=(args.base_url or os.getenv("BASE_URL", "https://auotam.net")).rstrip("/"),
        suppression_dir=Path(
            getattr(args, "suppression_dir", "")
            or os.getenv("SUPPRESSION_DIR", str(DEFAULT_SUPPRESSION_DIR))
        ),
        max_per_day_per_domain=int(
            os.getenv("MAX_PER_DAY_PER_DOMAIN", str(getattr(args, "max_per_day_per_domain", 50)))
        ),
        start_hour_est=args.start_hour_est,
        end_hour_est=args.end_hour_est,
        daily_cap=args.daily_cap,
        sends_per_second=args.sends_per_second,
        reply_to=_normalize_production_email(
            args.reply_to or os.getenv("REPLY_TO", os.getenv("AUOTAM_REPLY_TO", ""))
        )
        or _PRODUCTION_FROM_EMAIL,
    )


def template_for_segment(segment: str) -> Dict[str, str]:
    resolved = segment if segment in TEMPLATE_VARIANTS else "universal"
    variants = TEMPLATE_VARIANTS[resolved]
    variant_key = random.choice(["A", "B", "C"])
    selected = variants.get(variant_key) or variants["A"]
    return {"subject": selected["subject"], "body": selected["body"], "variant": variant_key}


def build_email(row: dict, to_email: str, base_url: str) -> Dict[str, str]:
    segment = (row.get("segment") or "").strip().lower()
    template = template_for_segment(segment)
    first_name = split_first_name(row.get("owner_name", ""))
    business_name = (row.get("business_name") or "").strip() or "your business"

    subject = template["subject"]
    body = template["body"].format(first_name=first_name, business_name=business_name)
    body = body + unsubscribe_footer(base_url, to_email)
    return {"subject": subject, "body": body, "variant": template.get("variant", "")}


def read_csv_rows(path: Path, max_rows: Optional[int] = None) -> Iterable[dict]:
    """Yield CSV rows in file order. If max_rows is set, stop after that many data rows (after header)."""
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        n = 0
        for row in reader:
            if max_rows is not None and n >= max_rows:
                break
            yield row
            n += 1


def sent_today_count(log_path: Path) -> int:
    from auotam import pg_store
    from auotam.db import database_url

    if database_url():
        return pg_store.sent_today_count_est()
    if not log_path.exists():
        return 0
    today = datetime.now(tz=EST).date().isoformat()
    count = 0
    with log_path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            if row.get("sent_date_est") == today and row.get("status") == "sent":
                count += 1
    return count


def append_log(log_path: Path, row: dict) -> None:
    from auotam import pg_store

    if pg_store.use_database():
        pg_store.insert_email_log_from_agent_row(row)
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not log_path.exists()
    with log_path.open("a", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def sent_today_to_recipient(log_path: Path, email: str) -> bool:
    """
    True if this recipient should be skipped for another send today.

    When DATABASE_URL is set, uses email_log (initial only, EST calendar day) via pg_store;
    otherwise scans the CSV log for any successful send today (any mail_kind).
    """
    from auotam import pg_store
    from auotam.db import database_url

    if database_url():
        return pg_store.recipient_sent_today_est(email)
    if not log_path.exists():
        return False
    today = datetime.now(tz=EST).date().isoformat()
    em = (email or "").strip().lower()
    with log_path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            if row.get("sent_date_est") != today or row.get("status") != "sent":
                continue
            if (row.get("email") or "").strip().lower() == em:
                return True
    return False


def _emails_with_any_sent_today_from_log(log_path: Path) -> Set[str]:
    """
    Emails with any successful send logged today (EST calendar date on row),
    matching sent_today_to_recipient() when DATABASE_URL is unset.
    """
    out: Set[str] = set()
    if not log_path.exists():
        return out
    today = datetime.now(tz=EST).date().isoformat()
    with log_path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            if row.get("sent_date_est") != today or row.get("status") != "sent":
                continue
            e = (row.get("email") or "").strip().lower()
            if e:
                out.add(e)
    return out


# First successful production send of each EST calendar day: BCC-style copy to this inbox (not logged).
DAILY_TEST_COPY_TO = "govind@auotam.com"
DAILY_TEST_COPY_SUBJECT_PREFIX = "[TEST COPY] "
DAILY_TEST_COPY_FLAG_FILE = ".email_agent_daily_test_copy_date"


def _daily_test_copy_flag_path(log_path: Path) -> Path:
    return log_path.parent / DAILY_TEST_COPY_FLAG_FILE


def _daily_test_copy_already_sent_for_date(log_path: Path, sent_date_est: str) -> bool:
    p = _daily_test_copy_flag_path(log_path)
    try:
        return p.read_text(encoding="utf-8").strip() == sent_date_est
    except OSError:
        return False


def _mark_daily_test_copy_sent_for_date(log_path: Path, sent_date_est: str) -> None:
    p = _daily_test_copy_flag_path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(sent_date_est + "\n", encoding="utf-8")


def maybe_send_daily_test_copy_after_production_send(
    cfg: Config,
    args: argparse.Namespace,
    ses: Optional[object],
    *,
    log_path: Path,
    ses_status_path: Path,
    domain_counts: Dict[str, int],
    today_iso: str,
    from_email: str,
    from_name: str,
    sending_domain: str,
    subject: str,
    body_text: str,
    company: str,
    name: str,
    segment: str,
    entity_detail_id: str,
    uei: str,
    mail_kind: str,
    template_variant: str,
) -> None:
    """
    Once per EST day: after a real production send succeeds, send the same body to DAILY_TEST_COPY_TO
    with subject prefixed by DAILY_TEST_COPY_SUBJECT_PREFIX. Uses _send_single_message (SendGrid or SES).
    Does not append to send log or domain counts. Tracked via a one-line date file beside the log CSV.
    """
    if args.dry_run:
        return
    if _daily_test_copy_already_sent_for_date(log_path, today_iso):
        return
    test_to = DAILY_TEST_COPY_TO.strip().lower()
    if not test_to or "@" not in test_to:
        return
    dom = test_to.split("@", 1)[1]
    ts_now = datetime.now(tz=EST)
    st, _mid, err = _send_single_message(
        cfg,
        args,
        ses,
        from_email=from_email,
        from_name=from_name,
        sending_domain=sending_domain,
        recipient_domain=dom,
        em_lower=test_to,
        subject=f"{DAILY_TEST_COPY_SUBJECT_PREFIX}{subject}",
        body_text=body_text,
        log_path=log_path,
        ses_status_path=ses_status_path,
        company=company,
        name=name,
        segment=segment,
        entity_detail_id=entity_detail_id,
        uei=uei,
        mail_kind="daily_test_copy",
        template_variant=template_variant,
        domain_counts=domain_counts,
        ts_now=ts_now,
        record_log_and_domain=False,
    )
    if st == "sent":
        _mark_daily_test_copy_sent_for_date(log_path, today_iso)
        print(f"Daily test copy sent to {test_to} (mirrors first send of {today_iso}, {mail_kind}).")
    else:
        print(f"Daily test copy to {test_to} failed (will retry on next successful send today): {err}")


def _send_single_message(
    cfg: Config,
    args: argparse.Namespace,
    ses: Optional[object],
    *,
    from_email: str,
    from_name: str,
    sending_domain: str,
    recipient_domain: str,
    em_lower: str,
    subject: str,
    body_text: str,
    log_path: Path,
    ses_status_path: Path,
    company: str,
    name: str,
    segment: str,
    entity_detail_id: str,
    uei: str,
    mail_kind: str,
    template_variant: str,
    domain_counts: Dict[str, int],
    ts_now: datetime,
    record_log_and_domain: bool = True,
) -> Tuple[str, str, str]:
    """
    Perform one outbound send + log row.
    Returns (status, message_id_or_empty, error_reason_or_empty).
    On success, increments domain_counts for recipient_domain and may append SES message status JSONL.
    Set record_log_and_domain=False for internal copies (e.g. daily test mirror) so caps/log stay clean.
    """
    message_id = ""
    status = "sent"
    reason = ""
    try:
        if args.dry_run:
            message_id = f"dryrun-{int(time.time()*1000)}"
        elif cfg.provider == "sendgrid":
            message_id = sendgrid_send_mail(
                api_key=cfg.sendgrid_api_key,
                from_email=from_email,
                from_name=from_name,
                to_email=em_lower,
                subject=subject,
                body_text=body_text,
                reply_to=cfg.reply_to,
                categories=["auotam-outbound", segment or "universal", sending_domain, mail_kind],
            )
        else:
            request = {
                "Source": formataddr((from_name, from_email)),
                "Destination": {"ToAddresses": [em_lower]},
                "Message": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}},
                },
            }
            if cfg.reply_to:
                request["ReplyToAddresses"] = [cfg.reply_to]
            if cfg.configuration_set:
                request["ConfigurationSetName"] = cfg.configuration_set
            response = ses.send_email(**request)
            message_id = response.get("MessageId", "")
            from auotam import pg_store

            if record_log_and_domain and not pg_store.use_database():
                ses_status_path.parent.mkdir(parents=True, exist_ok=True)
                with ses_status_path.open("a", encoding="utf-8") as sfp:
                    sfp.write(
                        json.dumps(
                            {
                                "message_id": message_id,
                                "to_email": em_lower,
                                "sent_at_est": ts_now.isoformat(),
                                "status": "sent",
                                "opened": False,
                                "bounced": False,
                                "replied": False,
                                "mail_kind": mail_kind,
                            }
                        )
                        + "\n"
                    )
        if record_log_and_domain:
            domain_counts[recipient_domain] = domain_counts.get(recipient_domain, 0) + 1
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        reason = str(exc)

    if record_log_and_domain:
        append_log(
            log_path,
            {
                "timestamp_est": ts_now.isoformat(),
                "sent_date_est": ts_now.date().isoformat(),
                "status": status,
                "reason": reason,
                "email": em_lower,
                "name": name,
                "company": company,
                "segment": segment,
                "mail_kind": mail_kind,
                "template_variant": template_variant,
                "sending_domain": sending_domain,
                "subject": subject,
                "body": body_text,
                "message_id": message_id,
                "entity_detail_id": entity_detail_id,
                "uei": uei,
            },
        )
    return status, message_id, reason


def send_batch(args: argparse.Namespace) -> None:
    """Email 1 (initial outreach) only — updates per-contact sequence state."""
    cfg = load_config(args)
    lock_from = bool(
        args.from_email or resolve_from_email_from_env() or os.getenv("SENDING_IDENTITIES", "").strip()
    )
    input_csv = Path(args.input_csv)
    log_path = Path(args.log_csv)
    seq_path = Path(args.sequence_status_jsonl or os.getenv("SEQUENCE_STATUS_JSONL", str(DEFAULT_SEQUENCE_STATUS_PATH)))
    ses_status_path = Path(args.status_jsonl)
    ses_status_path.parent.mkdir(parents=True, exist_ok=True)
    seed_suppression_files(cfg.suppression_dir)

    if cfg.provider == "sendgrid" and not args.dry_run and not cfg.sendgrid_api_key:
        raise SystemExit("Missing SendGrid API key. Set SENDGRID_API_KEY or --sendgrid-api-key.")

    if not cfg.from_email and not args.dry_run:
        raise SystemExit("Missing sender email. Set FROM_EMAIL / AUOTAM_FROM_EMAIL / --from-email.")

    if not input_csv.exists():
        raise SystemExit(f"Input CSV not found: {input_csv}")

    from auotam import pg_store

    if pg_store.use_database():
        pg_store.maybe_bootstrap_contacts_from_csv(input_csv)

    if not args.dry_run and not in_sending_window(cfg.start_hour_est, cfg.end_hour_est):
        raise SystemExit("Outside allowed sending window (Mon-Fri, EST business hours).")

    already_sent = sent_today_count(log_path)
    remaining = max(0, cfg.daily_cap - already_sent)
    if remaining == 0:
        print(f"Daily cap reached ({cfg.daily_cap}). Nothing to send.")
        return

    print(f"Already sent today: {already_sent}; remaining quota: {remaining}")
    ses = None
    if not args.dry_run and cfg.provider == "ses":
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise SystemExit("boto3 is required for SES live sending. Install with: pip3 install boto3") from exc
        ses = boto3.client("ses", region_name=cfg.aws_region)

    def resolve_from_identity() -> Tuple[str, str]:
        if lock_from:
            return cfg.from_email, cfg.from_name
        rotated = pick_identity()
        if rotated:
            return rotated
        return cfg.from_email, cfg.from_name

    suppression_cache: Set[str] = load_suppression_lists(cfg.suppression_dir)
    unsub_set, bounce_set = load_unsub_and_bounce_sets(cfg.suppression_dir)
    state = load_sequence_state(seq_path)
    domain_counts = load_domain_sent_today(log_path)
    seen_emails: Set[str] = set()
    today_d: date = datetime.now(tz=EST).date()
    today_iso = today_d.isoformat()

    sent = 0
    skipped = 0
    sleep_seconds = 1.0 / max(0.1, cfg.sends_per_second)

    for row in read_csv_rows(input_csv):
        if sent >= remaining:
            break

        to_email = (row.get("email") or "").strip()
        company = (row.get("business_name") or "").strip()
        name = (row.get("owner_name") or "").strip()
        segment = (row.get("segment") or "").strip().lower()
        ts_now = datetime.now(tz=EST)
        from_email, from_name = resolve_from_identity()
        sending_domain = sending_domain_from_from_email(from_email)

        def log_skip(reason: str) -> None:
            nonlocal skipped
            skipped += 1
            append_log(
                log_path,
                {
                    "timestamp_est": ts_now.isoformat(),
                    "sent_date_est": ts_now.date().isoformat(),
                    "status": "skipped",
                    "reason": reason,
                    "email": to_email,
                    "name": name,
                    "company": company,
                    "segment": segment,
                    "mail_kind": "initial",
                    "template_variant": "",
                    "sending_domain": sending_domain,
                    "subject": "",
                    "body": "",
                    "message_id": "",
                    "entity_detail_id": row.get("entity_detail_id", ""),
                    "uei": row.get("uei", ""),
                },
            )

        if not is_valid_email(to_email):
            log_skip("invalid_email")
            continue

        em_lower = to_email.lower()
        if em_lower in seen_emails:
            log_skip("duplicate_in_run")
            continue
        seen_emails.add(em_lower)
        row = merge_contact_website_from_db(row, em_lower)

        if is_suppressed(em_lower, suppression_cache):
            log_skip("suppressed")
            continue

        if is_role_address(em_lower):
            log_skip("role_address")
            continue

        if sent_today_to_recipient(log_path, em_lower):
            log_skip("already_sent_today")
            continue

        if pg_store.use_database():
            try:
                cid = pg_store.get_or_create_contact_id_for_row(
                    {
                        "email": em_lower,
                        "owner_name": name,
                        "business_name": company,
                        "segment": segment,
                        "website": (row.get("website") or "").strip(),
                    }
                )
                if pg_store.is_lead_status_blocked(cid):
                    log_skip("lead_won_or_not_interested")
                    continue
            except ValueError:
                log_skip("invalid_email")
                continue

        st = state.get(em_lower, default_record(em_lower))
        merge_list_flags_into_record(st, em_lower, unsub_set, bounce_set)
        if st.get("replied"):
            log_skip("replied")
            continue
        if st.get("unsubscribed") or st.get("bounced"):
            log_skip("unsubscribed_or_bounced")
            continue

        if is_dormant_cooldown_active(em_lower, cfg.suppression_dir, today_d) and not (st.get("email_1_sent") or ""):
            log_skip("dormant_cooldown")
            continue

        if (st.get("email_1_sent") or "").strip():
            if not (st.get("email_4_sent") or "").strip():
                log_skip("sequence_in_progress")
                continue
            if is_dormant_cooldown_active(em_lower, cfg.suppression_dir, today_d):
                log_skip("dormant_cooldown")
                continue
            st = default_record(em_lower)
            merge_list_flags_into_record(st, em_lower, unsub_set, bounce_set)

        recipient_domain = em_lower.split("@", 1)[1]
        if domain_counts.get(recipient_domain, 0) >= cfg.max_per_day_per_domain:
            log_skip("recipient_domain_cap")
            continue

        email_content = build_email(row, to_email=em_lower, base_url=cfg.base_url)
        status, _mid, _reason = _send_single_message(
            cfg,
            args,
            ses,
            from_email=from_email,
            from_name=from_name,
            sending_domain=sending_domain,
            recipient_domain=recipient_domain,
            em_lower=em_lower,
            subject=email_content["subject"],
            body_text=email_content["body"],
            log_path=log_path,
            ses_status_path=ses_status_path,
            company=company,
            name=name,
            segment=segment,
            entity_detail_id=str(row.get("entity_detail_id", "") or ""),
            uei=str(row.get("uei", "") or ""),
            mail_kind="initial",
            template_variant=email_content.get("variant", ""),
            domain_counts=domain_counts,
            ts_now=ts_now,
        )
        if status == "sent":
            sent += 1
            st["email_1_sent"] = today_iso
            st["last_sent_date_est"] = today_iso
            state[em_lower] = st
            save_sequence_record(seq_path, em_lower, st)
            maybe_send_daily_test_copy_after_production_send(
                cfg,
                args,
                ses,
                log_path=log_path,
                ses_status_path=ses_status_path,
                domain_counts=domain_counts,
                today_iso=today_iso,
                from_email=from_email,
                from_name=from_name,
                sending_domain=sending_domain,
                subject=email_content["subject"],
                body_text=email_content["body"],
                company=company,
                name=name,
                segment=segment,
                entity_detail_id=str(row.get("entity_detail_id", "") or ""),
                uei=str(row.get("uei", "") or ""),
                mail_kind="initial",
                template_variant=email_content.get("variant", "") or "",
            )
        time.sleep(sleep_seconds)

    print(f"Completed. sent={sent}, skipped={skipped}, log={log_path}")


def orchestrate_batch(args: argparse.Namespace) -> None:
    """
    One session: follow-ups 2→3→4 (by day since Email 1), then Email 1 for new/re-eligible contacts.
    Shared daily cap, same window / throttles / suppression as send.

    Use --max-rows N to cap how many leading CSV data rows are read (scheduler defaults via run_scheduler).
    Follow-ups still use full sequence state from the DB; only the CSV cohort for merge/index/initial pass is clipped.
    """
    cfg = load_config(args)
    lock_from = bool(
        args.from_email or resolve_from_email_from_env() or os.getenv("SENDING_IDENTITIES", "").strip()
    )
    input_csv = Path(args.input_csv)
    log_path = Path(args.log_csv)
    seq_path = Path(args.sequence_status_jsonl)
    ses_status_path = Path(args.ses_message_status_jsonl)
    ses_status_path.parent.mkdir(parents=True, exist_ok=True)
    seq_path.parent.mkdir(parents=True, exist_ok=True)
    seed_suppression_files(cfg.suppression_dir)

    if cfg.provider == "sendgrid" and not args.dry_run and not cfg.sendgrid_api_key:
        raise SystemExit("Missing SendGrid API key. Set SENDGRID_API_KEY or --sendgrid-api-key.")
    if not cfg.from_email and not args.dry_run:
        raise SystemExit("Missing sender email. Set FROM_EMAIL / AUOTAM_FROM_EMAIL / --from-email.")
    if not input_csv.exists():
        raise SystemExit(f"Input CSV not found: {input_csv}")

    from auotam import pg_store

    orch_max = args.max_rows if args.max_rows > 0 else None
    if orch_max:
        print(f"Orchestrate: CSV read limited to first {orch_max} data rows (--max-rows)", flush=True)

    if pg_store.use_database():
        pg_store.maybe_bootstrap_contacts_from_csv(input_csv, max_rows=orch_max)

    if not args.dry_run and not in_sending_window(cfg.start_hour_est, cfg.end_hour_est):
        raise SystemExit("Outside allowed sending window (Mon-Fri, EST business hours).")

    budget = max(0, cfg.daily_cap - sent_today_count(log_path))
    if budget == 0:
        print(f"Daily cap reached ({cfg.daily_cap}). Nothing to send.")
        return
    print(f"Orchestrate: daily budget remaining={budget}", flush=True)
    print(f"Orchestrate: sender from_email={cfg.from_email!r}", flush=True)

    ses = None
    if not args.dry_run and cfg.provider == "ses":
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise SystemExit("boto3 is required for SES live sending. Install with: pip3 install boto3") from exc
        ses = boto3.client("ses", region_name=cfg.aws_region)

    def resolve_from_identity() -> Tuple[str, str]:
        if lock_from:
            return cfg.from_email, cfg.from_name
        rotated = pick_identity()
        if rotated:
            return rotated
        return cfg.from_email, cfg.from_name

    suppression_cache: Set[str] = load_suppression_lists(cfg.suppression_dir)
    unsub_set, bounce_set = load_unsub_and_bounce_sets(cfg.suppression_dir)
    state = load_sequence_state(seq_path)
    for _em, st in state.items():
        merge_list_flags_into_record(st, _em, unsub_set, bounce_set)

    csv_by_email: Dict[str, dict] = {}
    for row in read_csv_rows(input_csv, orch_max):
        e = (row.get("email") or "").strip().lower()
        if is_valid_email(e):
            csv_by_email[e] = row

    print(
        f"Orchestrate: indexed {len(csv_by_email)} CSV rows, {len(state)} sequence records",
        flush=True,
    )

    domain_counts = load_domain_sent_today(log_path)
    today_d: date = datetime.now(tz=EST).date()
    today_iso = today_d.isoformat()
    sleep_seconds = 1.0 / max(0.1, cfg.sends_per_second)
    sent = 0
    skipped = 0
    seen_today: Set[str] = set()

    from auotam.db import database_url

    if pg_store.use_database():
        recipient_sent_today_cache: Set[str] = pg_store.emails_with_initial_sent_today_est()
    else:
        recipient_sent_today_cache = _emails_with_any_sent_today_from_log(log_path)

    dormant_since_map: Dict[str, date] = load_dormant_since(cfg.suppression_dir)

    def dormant_cooldown_active(em_lower: str) -> bool:
        ds = dormant_since_map.get(em_lower)
        if not ds:
            return False
        return (today_d - ds).days < DORMANT_COOLDOWN_DAYS

    blocked_lead_emails: Set[str] = set()
    if pg_store.use_database():
        blocked_lead_emails = pg_store.load_blocked_lead_emails_lower()
        print("Orchestrate: bulk-loading contact websites for CSV cohort...", flush=True)
        _wmap = pg_store.load_contact_websites_for_emails(csv_by_email.keys())
        for _em, _site in _wmap.items():
            _row = csv_by_email.get(_em)
            if _row is None or not _site:
                continue
            if not (_row.get("website") or "").strip():
                csv_by_email[_em] = {**_row, "website": _site}
        print(
            f"Orchestrate: cohort DB maps ready (blocked_lead={len(blocked_lead_emails)}, "
            f"websites_from_db={len(_wmap)})",
            flush=True,
        )

    orchestrate_initial_hard_skip: Set[str] = (
        set(suppression_cache) | set(recipient_sent_today_cache) | blocked_lead_emails
    )
    _ORCH_SKIP_LOG_CHUNK = 500
    skip_log_buffer: List[dict] = []

    def flush_orchestrate_skip_buffer() -> None:
        if not skip_log_buffer:
            return
        n_buf = len(skip_log_buffer)
        print(f"Orchestrate: flushing {n_buf} skip log rows to DB/CSV", flush=True)
        if pg_store.use_database():
            pg_store.insert_email_log_from_agent_rows(skip_log_buffer)
        else:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not log_path.exists()
            with log_path.open("a", encoding="utf-8", newline="") as fp:
                writer = csv.DictWriter(fp, fieldnames=LOG_FIELDS, extrasaction="ignore")
                if write_header:
                    writer.writeheader()
                for lr in skip_log_buffer:
                    writer.writerow(lr)
        skip_log_buffer.clear()

    def note_recipient_sent_today(em_l: str, kind: str) -> None:
        if database_url():
            if kind == "initial":
                recipient_sent_today_cache.add(em_l)
        else:
            recipient_sent_today_cache.add(em_l)

    def log_skip_orchestrate(
        em: str,
        row: Optional[dict],
        reason: str,
        mail_kind: str,
        from_email: str,
        from_name: str,
        sending_domain: str,
    ) -> None:
        nonlocal skipped
        skipped += 1
        r = row or {}
        ts_now = datetime.now(tz=EST)
        skip_log_buffer.append(
            {
                "timestamp_est": ts_now.isoformat(),
                "sent_date_est": ts_now.date().isoformat(),
                "status": "skipped",
                "reason": reason,
                "email": em,
                "name": (r.get("owner_name") or "").strip(),
                "company": (r.get("business_name") or "").strip(),
                "segment": (r.get("segment") or "").strip().lower(),
                "mail_kind": mail_kind,
                "template_variant": "",
                "sending_domain": sending_domain,
                "subject": "",
                "body": "",
                "message_id": "",
                "entity_detail_id": str(r.get("entity_detail_id", "") or ""),
                "uei": str(r.get("uei", "") or ""),
            }
        )
        if len(skip_log_buffer) >= _ORCH_SKIP_LOG_CHUNK:
            flush_orchestrate_skip_buffer()

    def pre_send_gates(
        em_lower: str,
        row: dict,
        mail_kind: str,
        from_email: str,
        from_name: str,
        sending_domain: str,
    ) -> Optional[str]:
        """Return skip reason or None if OK to send."""
        if not is_valid_email(em_lower):
            return "invalid_email"
        if em_lower in blocked_lead_emails:
            return "lead_won_or_not_interested"
        if is_suppressed(em_lower, suppression_cache):
            return "suppressed"
        if is_role_address(em_lower):
            return "role_address"
        if em_lower in recipient_sent_today_cache or em_lower in seen_today:
            return "already_sent_today"
        dom = em_lower.split("@", 1)[1]
        if domain_counts.get(dom, 0) >= cfg.max_per_day_per_domain:
            return "recipient_domain_cap"
        return None

    def run_followup_step(
        step: int,
        min_days_since_e1: int,
        prev_sent_key: Optional[str],
        sent_key: str,
        mail_kind: str,
    ) -> None:
        nonlocal budget, sent, skipped
        cands: List[Tuple[str, dict, date]] = []
        for email, st in state.items():
            merge_list_flags_into_record(st, email, unsub_set, bounce_set)
            if st.get("replied") or st.get("unsubscribed") or st.get("bounced"):
                continue
            if (st.get("last_sent_date_est") or "") == today_iso:
                continue
            d1 = parse_iso_date(st.get("email_1_sent"))
            if not d1:
                continue
            if (today_d - d1).days < min_days_since_e1:
                continue
            if (st.get(sent_key) or "").strip():
                continue
            if prev_sent_key and not (st.get(prev_sent_key) or "").strip():
                continue
            row = csv_by_email.get(email)
            if not row:
                continue
            cands.append((email, row, d1))
        cands.sort(key=lambda x: x[2])
        for email, row, _d1 in cands:
            if budget <= 0:
                return
            from_email, from_name = resolve_from_identity()
            sending_domain = sending_domain_from_from_email(from_email)
            sk = pre_send_gates(email, row, mail_kind, from_email, from_name, sending_domain)
            if sk:
                log_skip_orchestrate(email, row, sk, mail_kind, from_email, from_name, sending_domain)
                continue
            fn = split_first_name(row.get("owner_name", ""))
            co = (row.get("business_name") or "").strip() or "your business"
            tpl = build_followup(step, fn, co)
            body = tpl["body"] + unsubscribe_footer(cfg.base_url, email)
            ts_now = datetime.now(tz=EST)
            dom = email.split("@", 1)[1]
            status, _mid, _err = _send_single_message(
                cfg,
                args,
                ses,
                from_email=from_email,
                from_name=from_name,
                sending_domain=sending_domain,
                recipient_domain=dom,
                em_lower=email,
                subject=tpl["subject"],
                body_text=body,
                log_path=log_path,
                ses_status_path=ses_status_path,
                company=co,
                name=(row.get("owner_name") or "").strip(),
                segment=(row.get("segment") or "").strip().lower(),
                entity_detail_id=str(row.get("entity_detail_id", "") or ""),
                uei=str(row.get("uei", "") or ""),
                mail_kind=mail_kind,
                template_variant="",
                domain_counts=domain_counts,
                ts_now=ts_now,
            )
            if status == "failed" and _is_ses_unverified_identity_error(_err):
                raise SystemExit(
                    f"SES rejected sender identity ({from_email!r}): {_err}. "
                    "Verify the address/domain in SES or set AUOTAM_FROM_EMAIL to a verified identity."
                )
            if status == "sent":
                budget -= 1
                sent += 1
                seen_today.add(email)
                note_recipient_sent_today(email, mail_kind)
                st = state.setdefault(email, default_record(email))
                merge_list_flags_into_record(st, email, unsub_set, bounce_set)
                st[sent_key] = today_iso
                st["last_sent_date_est"] = today_iso
                if step == 4 and not st.get("replied"):
                    append_dormant(email, today_d, cfg.suppression_dir)
                    st["dormant"] = True
                save_sequence_record(seq_path, email, st)
                maybe_send_daily_test_copy_after_production_send(
                    cfg,
                    args,
                    ses,
                    log_path=log_path,
                    ses_status_path=ses_status_path,
                    domain_counts=domain_counts,
                    today_iso=today_iso,
                    from_email=from_email,
                    from_name=from_name,
                    sending_domain=sending_domain,
                    subject=tpl["subject"],
                    body_text=body,
                    company=co,
                    name=(row.get("owner_name") or "").strip(),
                    segment=(row.get("segment") or "").strip().lower(),
                    entity_detail_id=str(row.get("entity_detail_id", "") or ""),
                    uei=str(row.get("uei", "") or ""),
                    mail_kind=mail_kind,
                    template_variant="",
                )
                time.sleep(sleep_seconds)

    try:
        run_followup_step(2, 5, None, "email_2_sent", "followup_2")
        run_followup_step(3, 10, "email_2_sent", "email_3_sent", "followup_3")
        run_followup_step(4, 16, "email_3_sent", "email_4_sent", "followup_4")

        print(
            "Orchestrate: follow-ups done; about to sort csv_by_email for initial pass",
            flush=True,
        )
        _initial_csv_items = sorted(csv_by_email.items(), key=lambda kv: kv[0])
        print(
            f"Orchestrate: sort complete ({len(_initial_csv_items)} rows); entering initial enumerate loop",
            flush=True,
        )
        _orch_diag_first_candidate = True
        for _n, (em_lower, row) in enumerate(_initial_csv_items, start=1):
            if _n == 1:
                print(
                    f"Orchestrate: initial loop iteration 1 started (email={em_lower}, "
                    f"hard_skip={em_lower in orchestrate_initial_hard_skip})",
                    flush=True,
                )
            elif _n % 500 == 0:
                print(
                    f"Orchestrate: initial CSV scan row {_n} (sent={sent}, skipped={skipped}, budget={budget})",
                    flush=True,
                )
            if budget <= 0:
                break
            if em_lower in orchestrate_initial_hard_skip:
                if _n == 1:
                    print("Orchestrate: iteration 1 → hard_skip continue", flush=True)
                continue
            company = (row.get("business_name") or "").strip()
            name = (row.get("owner_name") or "").strip()
            segment = (row.get("segment") or "").strip().lower()
            ts_now = datetime.now(tz=EST)
            from_email, from_name = resolve_from_identity()
            sending_domain = sending_domain_from_from_email(from_email)

            if _orch_diag_first_candidate:
                _orch_diag_first_candidate = False
                print(
                    f"Orchestrate: first non-hard-skip row {_n} ({em_lower}); calling pre_send_gates",
                    flush=True,
                )
            _t_gates = time.monotonic()
            sk = pre_send_gates(em_lower, row, "initial", from_email, from_name, sending_domain)
            if _n <= 3 or (sent == 0 and skipped < 5):
                print(
                    f"Orchestrate: row {_n} pre_send_gates → {sk!r} ({time.monotonic() - _t_gates:.3f}s)",
                    flush=True,
                )
            if sk:
                log_skip_orchestrate(em_lower, row, sk, "initial", from_email, from_name, sending_domain)
                continue

            st = state.get(em_lower, default_record(em_lower))
            merge_list_flags_into_record(st, em_lower, unsub_set, bounce_set)
            if st.get("replied"):
                log_skip_orchestrate(em_lower, row, "replied", "initial", from_email, from_name, sending_domain)
                continue
            if st.get("unsubscribed") or st.get("bounced"):
                log_skip_orchestrate(
                    em_lower, row, "unsubscribed_or_bounced", "initial", from_email, from_name, sending_domain
                )
                continue
            if dormant_cooldown_active(em_lower) and not (st.get("email_1_sent") or ""):
                log_skip_orchestrate(em_lower, row, "dormant_cooldown", "initial", from_email, from_name, sending_domain)
                continue
            if (st.get("email_1_sent") or "").strip():
                if not (st.get("email_4_sent") or "").strip():
                    log_skip_orchestrate(
                        em_lower, row, "sequence_in_progress", "initial", from_email, from_name, sending_domain
                    )
                    continue
                if dormant_cooldown_active(em_lower):
                    log_skip_orchestrate(em_lower, row, "dormant_cooldown", "initial", from_email, from_name, sending_domain)
                    continue
                st = default_record(em_lower)
                merge_list_flags_into_record(st, em_lower, unsub_set, bounce_set)

            recipient_domain = em_lower.split("@", 1)[1]
            email_content = build_email(row, to_email=em_lower, base_url=cfg.base_url)
            if sent == 0:
                print(
                    f"Orchestrate: row {_n} before _send_single_message ({em_lower}); "
                    f"provider={cfg.provider} dry_run={args.dry_run}",
                    flush=True,
                )
            _t_send = time.monotonic()
            status, _mid, _reason = _send_single_message(
                cfg,
                args,
                ses,
                from_email=from_email,
                from_name=from_name,
                sending_domain=sending_domain,
                recipient_domain=recipient_domain,
                em_lower=em_lower,
                subject=email_content["subject"],
                body_text=email_content["body"],
                log_path=log_path,
                ses_status_path=ses_status_path,
                company=company,
                name=name,
                segment=segment,
                entity_detail_id=str(row.get("entity_detail_id", "") or ""),
                uei=str(row.get("uei", "") or ""),
                mail_kind="initial",
                template_variant=email_content.get("variant", ""),
                domain_counts=domain_counts,
                ts_now=ts_now,
            )
            if sent == 0:
                print(
                    f"Orchestrate: row {_n} after _send_single_message status={status!r} "
                    f"reason={_reason!r} ({time.monotonic() - _t_send:.3f}s)",
                    flush=True,
                )
            if status == "failed" and _is_ses_unverified_identity_error(_reason):
                raise SystemExit(
                    f"SES rejected sender identity ({from_email!r}): {_reason}. "
                    "Verify the address/domain in SES or set AUOTAM_FROM_EMAIL to a verified identity."
                )
            if status == "sent":
                budget -= 1
                sent += 1
                seen_today.add(em_lower)
                note_recipient_sent_today(em_lower, "initial")
                st = state.setdefault(em_lower, default_record(em_lower))
                merge_list_flags_into_record(st, em_lower, unsub_set, bounce_set)
                st["email_1_sent"] = today_iso
                st["last_sent_date_est"] = today_iso
                save_sequence_record(seq_path, em_lower, st)
                maybe_send_daily_test_copy_after_production_send(
                    cfg,
                    args,
                    ses,
                    log_path=log_path,
                    ses_status_path=ses_status_path,
                    domain_counts=domain_counts,
                    today_iso=today_iso,
                    from_email=from_email,
                    from_name=from_name,
                    sending_domain=sending_domain,
                    subject=email_content["subject"],
                    body_text=email_content["body"],
                    company=company,
                    name=name,
                    segment=segment,
                    entity_detail_id=str(row.get("entity_detail_id", "") or ""),
                    uei=str(row.get("uei", "") or ""),
                    mail_kind="initial",
                    template_variant=email_content.get("variant", "") or "",
                )
                if budget <= 0:
                    break
                time.sleep(sleep_seconds)

        print(f"Orchestrate completed. sent={sent}, skipped={skipped}, log={log_path}, sequence={seq_path}")
    finally:
        flush_orchestrate_skip_buffer()


def ingest_events(args: argparse.Namespace) -> None:
    """
    Ingest SES SNS event lines (one JSON per line) and update status jsonl.
    Expected eventType values include: Delivery, Bounce, Complaint, Open, Click.
    """
    from auotam import pg_store

    source = Path(args.sns_jsonl)
    status_path = Path(args.status_jsonl)

    if not source.exists():
        raise SystemExit(f"SNS JSONL file not found: {source}")

    if pg_store.use_database():
        n = pg_store.ingest_ses_events_from_jsonl(source)
        print(f"Ingested events. Updated email_log rows (approx): {n}")
        return

    statuses: Dict[str, dict] = {}
    if status_path.exists():
        with status_path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                mid = record.get("message_id")
                if mid:
                    statuses[mid] = record

    with source.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            evt = json.loads(line)

            # SNS wrapper support
            if "Message" in evt and isinstance(evt["Message"], str):
                try:
                    evt = json.loads(evt["Message"])
                except json.JSONDecodeError:
                    pass

            message_id = (evt.get("mail") or {}).get("messageId")
            if not message_id:
                continue

            rec = statuses.get(message_id, {"message_id": message_id})
            event_type = (evt.get("eventType") or "").lower()
            if event_type == "bounce":
                rec["bounced"] = True
                rec["status"] = "bounced"
            elif event_type == "open":
                rec["opened"] = True
            elif event_type in {"delivery", "send"}:
                rec["status"] = "delivered"
            elif event_type == "complaint":
                rec["status"] = "complaint"
            elif event_type == "click":
                rec["clicked"] = True
            rec["last_event_type"] = event_type
            rec["last_event_at"] = datetime.now(tz=EST).isoformat()
            statuses[message_id] = rec

    with status_path.open("w", encoding="utf-8") as fp:
        for rec in statuses.values():
            fp.write(json.dumps(rec) + "\n")

    print(f"Ingested events. Updated statuses: {len(statuses)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AUOTAM outbound email sending agent")
    sub = parser.add_subparsers(dest="command", required=True)

    send = sub.add_parser("send", help="Send emails from segmented CSV")
    send.add_argument("--input-csv", required=True, help="Input contacts CSV path")
    send.add_argument("--log-csv", default="data/logs/send_log.csv", help="Send log CSV path")
    send.add_argument(
        "--status-jsonl",
        default="data/events/status.jsonl",
        help="SES message-id JSONL (event tracking), not sequence state",
    )
    send.add_argument(
        "--sequence-status-jsonl",
        default=os.getenv("SEQUENCE_STATUS_JSONL", str(DEFAULT_SEQUENCE_STATUS_PATH)),
        help="Per-contact 4-email sequence state (JSONL)",
    )
    send.add_argument(
        "--provider",
        default="",
        help="ses (default) or sendgrid — use sendgrid only if EMAIL_PROVIDER=sendgrid and key set",
    )
    send.add_argument("--sendgrid-api-key", default="", help="SendGrid API key (or SENDGRID_API_KEY)")
    send.add_argument("--base-url", default="", help="Public site base URL for unsubscribe links")
    send.add_argument(
        "--suppression-dir",
        default="",
        help="Directory containing suppression CSVs (default: data/suppression)",
    )
    send.add_argument(
        "--max-per-day-per-domain",
        type=int,
        default=50,
        help="Max successful sends per recipient domain per EST day (default: 50)",
    )
    send.add_argument("--aws-region", default="", help="AWS region, e.g. us-east-1")
    send.add_argument("--from-email", default="", help="Verified sender email")
    send.add_argument("--from-name", default="Govind Chauhan", help="Sender display name")
    send.add_argument("--reply-to", default="", help="Reply-to inbox")
    send.add_argument("--configuration-set", default="", help="SES configuration set for events")
    send.add_argument("--start-hour-est", type=int, default=9, help="EST start hour (inclusive)")
    send.add_argument("--end-hour-est", type=int, default=17, help="EST end hour (exclusive)")
    send.add_argument("--daily-cap", type=int, default=6000, help="Max sends per EST day")
    send.add_argument("--sends-per-second", type=float, default=1.0, help="Rate limit (max sends/sec)")
    send.add_argument("--dry-run", action="store_true", help="Render and log only, do not call provider")

    orch = sub.add_parser(
        "orchestrate",
        help="Run follow-ups 2–4 then initial sends under one shared daily cap",
    )
    orch.add_argument("--input-csv", required=True, help="Input contacts CSV path")
    orch.add_argument("--log-csv", default="data/logs/send_log.csv", help="Send log CSV path")
    orch.add_argument(
        "--sequence-status-jsonl",
        default=os.getenv("SEQUENCE_STATUS_JSONL", str(DEFAULT_SEQUENCE_STATUS_PATH)),
        help="Per-contact sequence state JSONL",
    )
    orch.add_argument(
        "--ses-message-status-jsonl",
        default="data/events/status.jsonl",
        help="SES outbound message-id JSONL",
    )
    orch.add_argument(
        "--provider",
        default="",
        help="ses (default) or sendgrid",
    )
    orch.add_argument("--sendgrid-api-key", default="", help="SendGrid API key")
    orch.add_argument("--base-url", default="", help="Public base URL for unsubscribe links")
    orch.add_argument("--suppression-dir", default="", help="Suppression CSV directory")
    orch.add_argument("--max-per-day-per-domain", type=int, default=50)
    orch.add_argument("--aws-region", default="", help="AWS region")
    orch.add_argument("--from-email", default="", help="From address")
    orch.add_argument("--from-name", default="Govind Chauhan", help="From display name")
    orch.add_argument("--reply-to", default="", help="Reply-To inbox")
    orch.add_argument("--configuration-set", default="", help="SES configuration set")
    orch.add_argument("--start-hour-est", type=int, default=9)
    orch.add_argument("--end-hour-est", type=int, default=17)
    orch.add_argument("--daily-cap", type=int, default=6000)
    orch.add_argument("--sends-per-second", type=float, default=1.0)
    orch.add_argument("--dry-run", action="store_true")
    orch.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="If >0, only read the first N data rows from the input CSV (indexing, bootstrap, initial pass). "
        "0 = read entire file. Scheduler defaults to passing 5000.",
    )

    ingest = sub.add_parser("ingest-events", help="Ingest SES SNS event JSONL")
    ingest.add_argument("--sns-jsonl", required=True, help="SNS payload JSONL path")
    ingest.add_argument(
        "--status-jsonl",
        default="data/events/status.jsonl",
        help="Message status JSONL path",
    )

    return parser


def main() -> None:
    print(f"[MODE] {'DB' if os.environ.get('DATABASE_URL') else 'CSV'}")
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "send":
        send_batch(args)
        return
    if args.command == "orchestrate":
        orchestrate_batch(args)
        return
    if args.command == "ingest-events":
        ingest_events(args)
        return
    raise SystemExit("Unknown command")


if __name__ == "__main__":
    main()
