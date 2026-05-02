"""
Optional multi-sending-identity rotation (future scaling).

Set env SENDING_IDENTITIES to a JSON array of objects:
[
  {"from_email":"hello@auotam.net","from_name":"Govind Chauhan","weight":1},
  {"from_email":"outreach@auotam.net","from_name":"Govind Chauhan","weight":1}
]

If unset, callers should use a single FROM_EMAIL.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class SendingIdentity:
    from_email: str
    from_name: str
    weight: int = 1


def load_identities() -> Optional[List[SendingIdentity]]:
    raw = os.getenv("SENDING_IDENTITIES", "").strip()
    if not raw:
        return None
    data = json.loads(raw)
    identities: List[SendingIdentity] = []
    for item in data:
        identities.append(
            SendingIdentity(
                from_email=str(item["from_email"]),
                from_name=str(item.get("from_name") or "Govind Chauhan"),
                weight=int(item.get("weight") or 1),
            )
        )
    return identities


def pick_identity() -> Optional[Tuple[str, str]]:
    ids = load_identities()
    if not ids:
        return None
    weights = [max(1, i.weight) for i in ids]
    choice = random.choices(ids, weights=weights, k=1)[0]
    return choice.from_email, choice.from_name
