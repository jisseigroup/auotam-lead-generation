"""Follow-up emails 2–4 (single version each). Plain text + caller adds unsubscribe footer."""

from __future__ import annotations

from typing import Dict


def build_followup(step: int, first_name: str, business_name: str) -> Dict[str, str]:
    fn = first_name or "there"
    bn = (business_name or "").strip() or "your business"
    if step == 2:
        return {
            "subject": f"Is this your day, {fn}?",
            "body": (
                f"Hi {fn},\n"
                "Most business owners I work with say the same thing — they're so deep in daily "
                "operations they never get to the work that actually grows the business.\n"
                "We've fixed this across housing, eCommerce, defense, healthcare and more.\n"
                f"Worth 30 minutes to see if we can do the same for {bn}?\n"
                "auotam.com/book\n"
                "Govind\n"
                "AUOTAM"
            ),
        }
    if step == 3:
        return {
            "subject": f"Quick question, {fn}",
            "body": (
                f"Hi {fn},\n"
                "Do you know exactly which parts of your day could be automated?\n"
                "Most business owners don't — until we sit down and map it out together. That's when it clicks.\n"
                "I'm offering free workflow reviews this month. 30 minutes. No cost. No obligation.\n"
                "auotam.com/book\n"
                "Govind\n"
                "AUOTAM"
            ),
        }
    if step == 4:
        return {
            "subject": f"Last one, {fn}",
            "body": (
                f"Hi {fn},\n"
                "Every day spent doing work you shouldn't be doing is a day your business didn't grow.\n"
                "That's fixable. I've done it across dozens of businesses.\n"
                "If the timing is ever right — I'm one conversation away.\n"
                "auotam.com/book\n"
                "Govind\n"
                "AUOTAM"
            ),
        }
    raise ValueError(f"Invalid follow-up step: {step}")
