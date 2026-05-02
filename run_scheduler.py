#!/usr/bin/env python3
"""
Weekday EST scheduler for AUOTAM email sending agent.

It wraps `email_agent.py send` and runs in a loop:
- Mon-Fri only
- EST business-hour gate
- Hourly pacing toward daily target
- Cost guard: before each send attempt, queries AWS Cost Explorer (MTD UnblendedCost);
  pauses if spend >= ceiling (default $50). On API failure, logs warning and allows send.

Example:
  python3 run_scheduler.py --input-csv output/sba/all_businesses.csv --dry-run
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from auotam.cost_guard import session_cost_check


EST = ZoneInfo("America/New_York")


def now_est() -> datetime:
    return datetime.now(tz=EST)


def is_window_open(start_hour: int, end_hour: int, now: datetime | None = None) -> bool:
    dt = now or now_est()
    if dt.weekday() >= 5:
        return False
    return start_hour <= dt.hour < end_hour


def sent_today(log_csv: Path) -> int:
    if not log_csv.exists():
        return 0
    today = now_est().date().isoformat()
    count = 0
    with log_csv.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            if row.get("sent_date_est") == today and row.get("status") == "sent":
                count += 1
    return count


def hourly_target(daily_target: int, start_hour: int, end_hour: int) -> int:
    hours = max(1, end_hour - start_hour)
    return max(1, daily_target // hours)


def seconds_until_next_check(interval_seconds: int) -> int:
    return max(10, interval_seconds)


def run_send_once(args: argparse.Namespace, per_run_cap: int) -> int:
    cmd = [
        "python3",
        "email_agent.py",
        "send",
        "--input-csv",
        args.input_csv,
        "--log-csv",
        args.log_csv,
        "--status-jsonl",
        args.status_jsonl,
        "--daily-cap",
        str(args.daily_target),
        "--start-hour-est",
        str(args.start_hour_est),
        "--end-hour-est",
        str(args.end_hour_est),
        "--sends-per-second",
        str(args.sends_per_second),
    ]

    if args.aws_region:
        cmd += ["--aws-region", args.aws_region]
    if args.from_email:
        cmd += ["--from-email", args.from_email]
    if args.from_name:
        cmd += ["--from-name", args.from_name]
    if args.reply_to:
        cmd += ["--reply-to", args.reply_to]
    if args.configuration_set:
        cmd += ["--configuration-set", args.configuration_set]
    cmd += ["--provider", args.email_provider]
    if args.dry_run:
        cmd += ["--dry-run"]

    # Limit this invocation to per-run chunk by temporarily lowering daily cap.
    # We do that by passing cap = already_sent + per_run_cap.
    # The caller computes this cap before invoking us.
    cmd[cmd.index("--daily-cap") + 1] = str(per_run_cap)

    print(f"[{now_est().isoformat()}] Executing send run...")
    result = subprocess.run(cmd, check=False)
    return result.returncode


def scheduler_loop(args: argparse.Namespace) -> None:
    log_csv = Path(args.log_csv)
    per_hour = hourly_target(args.daily_target, args.start_hour_est, args.end_hour_est)
    print(
        f"Scheduler started. daily_target={args.daily_target}, "
        f"hourly_target={per_hour}, window={args.start_hour_est}:00-{args.end_hour_est}:00 EST"
    )

    while True:
        current = now_est()
        if not is_window_open(args.start_hour_est, args.end_hour_est, current):
            print(f"[{current.isoformat()}] Outside window. Sleeping {args.poll_interval_seconds}s.")
            time.sleep(seconds_until_next_check(args.poll_interval_seconds))
            continue

        already = sent_today(log_csv)
        if already >= args.daily_target:
            print(f"[{current.isoformat()}] Daily target reached ({already}/{args.daily_target}). Sleeping.")
            time.sleep(seconds_until_next_check(args.poll_interval_seconds))
            continue

        cost_log = Path(args.cost_log_path)
        ceiling = float(
            os.getenv("COST_GUARD_CEILING_USD", str(args.cost_ceiling_usd))
        )
        allowed, mtd_spend, _reason = session_cost_check(
            ceiling_usd=ceiling,
            log_path=cost_log,
            disabled=args.disable_cost_guard,
        )
        if not allowed:
            spend_note = f"${mtd_spend:.2f}" if mtd_spend is not None else "unknown"
            print(
                f"[{current.isoformat()}] Sending paused by cost guard "
                f"(MTD≈{spend_note}). Sleeping {args.poll_interval_seconds}s."
            )
            time.sleep(seconds_until_next_check(args.poll_interval_seconds))
            continue

        this_run_cap = min(args.daily_target, already + per_hour)
        rc = run_send_once(args, per_run_cap=this_run_cap)
        if rc != 0:
            print(f"[{now_est().isoformat()}] Send run failed with exit code {rc}.")
        else:
            updated = sent_today(log_csv)
            print(f"[{now_est().isoformat()}] Progress: {updated}/{args.daily_target}")

        if args.once:
            print("Exiting because --once was set.")
            return

        time.sleep(seconds_until_next_check(args.poll_interval_seconds))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run AUOTAM weekday sending scheduler")
    p.add_argument("--input-csv", required=True, help="Input contacts CSV")
    p.add_argument("--log-csv", default="data/logs/send_log.csv", help="Send log CSV path")
    p.add_argument("--status-jsonl", default="data/events/status.jsonl", help="Status JSONL path")
    p.add_argument("--daily-target", type=int, default=6000, help="Target sends per EST weekday")
    p.add_argument("--start-hour-est", type=int, default=9, help="Window start hour EST")
    p.add_argument("--end-hour-est", type=int, default=17, help="Window end hour EST")
    p.add_argument("--sends-per-second", type=float, default=1.0, help="Max sends per second")
    p.add_argument("--poll-interval-seconds", type=int, default=3600, help="Loop interval")
    p.add_argument("--aws-region", default="", help="AWS region")
    p.add_argument("--from-email", default="", help="SES verified sender")
    p.add_argument("--from-name", default="Govind Chauhan", help="Display sender name")
    p.add_argument("--reply-to", default="", help="Reply-to inbox")
    p.add_argument("--configuration-set", default="", help="SES config set")
    p.add_argument("--dry-run", action="store_true", help="Do not call SES")
    p.add_argument("--once", action="store_true", help="Run one cycle then exit")
    p.add_argument(
        "--email-provider",
        default=os.getenv("EMAIL_PROVIDER", "ses").strip().lower() or "ses",
        help="ses (default) or sendgrid — production uses SES unless overridden",
    )
    p.add_argument(
        "--cost-ceiling-usd",
        type=float,
        default=float(os.getenv("COST_GUARD_CEILING_USD", "50")),
        help="Pause sending when MTD AWS UnblendedCost >= this amount",
    )
    p.add_argument(
        "--cost-log-path",
        default=os.getenv("COST_GUARD_LOG_PATH", "data/logs/cost_guard.jsonl"),
        help="Append-only JSON log for each cost check",
    )
    p.add_argument(
        "--disable-cost-guard",
        action="store_true",
        help="Skip Cost Explorer check (still logs if you only use env DISABLE_COST_GUARD)",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    scheduler_loop(args)


if __name__ == "__main__":
    main()
