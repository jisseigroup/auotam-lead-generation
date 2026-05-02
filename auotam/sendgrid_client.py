"""
SendGrid v3 Mail Send via HTTPS (stdlib only).

https://api.sendgrid.com/v3/mail/send
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import List, Optional


def send_mail(
    api_key: str,
    from_email: str,
    from_name: str,
    to_email: str,
    subject: str,
    body_text: str,
    reply_to: Optional[str] = None,
    categories: Optional[List[str]] = None,
) -> str:
    payload: dict = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body_text}],
    }
    if reply_to:
        payload["reply_to"] = {"email": reply_to}
    if categories:
        payload["categories"] = categories

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            # SendGrid returns 202 with empty body; message id in headers when present
            return resp.headers.get("X-Message-Id", "") or "sendgrid-accepted"
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"SendGrid HTTP {e.code}: {err_body}") from e
