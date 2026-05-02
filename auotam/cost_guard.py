"""
AWS Cost Explorer — month-to-date UnblendedCost guard for the scheduler.

Uses the Cost Explorer API (ce) in us-east-1 only (AWS requirement).
IAM needed on the runtime identity: ce:GetCostAndUsage (e.g. AWSBillingReadOnlyAccess or a custom policy).

On any API failure or unexpected error: allow sending (log warning) so billing tooling cannot brick outreach.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_LOG_PATH = Path("data/logs/cost_guard.jsonl")


def append_cost_log(log_path: Path, record: Dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, default=str) + "\n")


def _fetch_mtd_unblended_usd() -> Tuple[Optional[float], str]:
    """Return (amount_usd, status_tag). amount_usd None if unavailable."""
    try:
        import boto3  # type: ignore
    except ImportError:
        return None, "boto3_missing"

    ce = boto3.client("ce", region_name="us-east-1")
    now = datetime.now(timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
    # End date is exclusive; +1 day includes all usage through "today" in UTC.
    period_end = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": period_start, "End": period_end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
    )
    total = 0.0
    rows = resp.get("ResultsByTime") or []
    if not rows:
        return 0.0, "ok_empty"
    for entry in rows:
        amt = (entry.get("Total") or {}).get("UnblendedCost", {}).get("Amount", "0")
        total += float(amt)
    return total, "ok"


def session_cost_check(
    ceiling_usd: float,
    log_path: Optional[Path] = None,
    *,
    disabled: bool = False,
) -> Tuple[bool, Optional[float], str]:
    """
    Run once per scheduler session before invoking email_agent.

    Returns:
      (allowed_to_send, mtd_spend_usd_or_none, short_reason)
    """
    path = log_path or DEFAULT_LOG_PATH
    ts = datetime.now(timezone.utc).isoformat()
    base: Dict[str, Any] = {
        "timestamp_utc": ts,
        "ceiling_usd": ceiling_usd,
    }

    if disabled or os.getenv("DISABLE_COST_GUARD", "").strip() in ("1", "true", "yes"):
        base["decision"] = "allowed_guard_disabled"
        append_cost_log(path, base)
        print(f"[cost_guard] Guard disabled; proceeding.")
        return True, None, "guard_disabled"

    try:
        spend, api_tag = _fetch_mtd_unblended_usd()
        base["api_status"] = api_tag
        base["mtd_unblended_cost_usd"] = spend

        if spend is None:
            base["decision"] = "allowed_api_unavailable"
            append_cost_log(path, base)
            print(f"[cost_guard] WARNING: could not determine MTD spend ({api_tag}); allowing send.")
            return True, None, api_tag

        if spend >= ceiling_usd:
            base["decision"] = "paused_over_ceiling"
            append_cost_log(path, base)
            print(
                f"[cost_guard] PAUSE: Month-to-date AWS cost ${spend:.2f} "
                f">= ceiling ${ceiling_usd:.2f}. Not sending this session."
            )
            return False, spend, "over_ceiling"

        base["decision"] = "allowed_under_ceiling"
        append_cost_log(path, base)
        print(f"[cost_guard] OK: Month-to-date AWS cost ${spend:.2f} < ceiling ${ceiling_usd:.2f}.")
        return True, spend, "under_ceiling"

    except Exception as exc:  # noqa: BLE001 — intentional fail-open
        base["decision"] = "allowed_on_exception"
        base["error"] = str(exc)
        append_cost_log(path, base)
        print(f"[cost_guard] WARNING: Cost Explorer check failed ({exc!r}); allowing send.")
        return True, None, f"exception:{exc}"
