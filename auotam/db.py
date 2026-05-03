"""PostgreSQL connection for AUOTAM CRM-backed persistence (optional via DATABASE_URL)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator, Optional


def database_url() -> Optional[str]:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    return url or None


@contextmanager
def get_connection() -> Generator[object, None, None]:
    import psycopg2  # noqa: PLC0415 — optional until DATABASE_URL is used

    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg2.connect(url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
