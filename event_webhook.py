#!/usr/bin/env python3
"""
SendGrid Event Webhook + public unsubscribe endpoint.

Run (dev):
  pip3 install flask
  export SENDGRID_WEBHOOK_SECRET=optional_shared_secret
  export BASE_URL=https://auotam.net
  python3 event_webhook.py

Configure SendGrid Event Webhook to POST to:
  https://auotam.net/webhook/sendgrid

Unsubscribe link (served by this app, not redirect-only):
  GET /unsubscribe?e=<urlsafe_base64_email>
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, jsonify, request

from auotam.suppression import add_to_suppression, seed_suppression_files

APP = Flask(__name__)

EVENTS_PATH = Path("data/events/sendgrid_events.jsonl")
WEBHOOK_SECRET = os.getenv("SENDGRID_WEBHOOK_SECRET", "").strip()


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(obj, default=str) + "\n")


def _decode_email_token(token: str) -> str:
    padded = token + "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return raw.decode("utf-8", errors="strict").strip().lower()


@APP.get("/unsubscribe")
def unsubscribe():
    token = (request.args.get("e") or "").strip()
    if not token:
        return "<p>Missing token.</p>", 400
    try:
        email = _decode_email_token(token)
    except Exception:
        return "<p>Invalid unsubscribe link.</p>", 400
    if "@" not in email:
        return "<p>Invalid email.</p>", 400
    add_to_suppression(email, "unsubscribe")
    return (
        "<html><body><p>You have been unsubscribed.</p>"
        "<p>If this was a mistake, reply to the email and we will help.</p></body></html>"
    )


@APP.post("/webhook/sendgrid")
def sendgrid_webhook():
    if WEBHOOK_SECRET:
        # SendGrid supports signing secret in some setups; basic shared-secret header gate.
        if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=False)
    _append_jsonl(
        EVENTS_PATH,
        {"received_at": datetime.utcnow().isoformat() + "Z", "payload": payload},
    )

    events: List[Dict[str, Any]] = payload if isinstance(payload, list) else [payload]
    for evt in events:
        et = str(evt.get("event") or evt.get("eventType") or "").lower()
        email = str(evt.get("email") or evt.get("recipient") or "").strip().lower()
        if not email:
            continue
        if et in ("bounce", "dropped", "spamreport", "unsubscribe"):
            add_to_suppression(email, et)
        elif et in ("spam_complaint", "complaint"):
            add_to_suppression(email, "complaint")

    return jsonify({"ok": True})


def main() -> None:
    seed_suppression_files()
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("PORT", "8080"))
    APP.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
