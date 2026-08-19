"""Backend-api calls with retry, re-auth, and page recovery."""

from __future__ import annotations

import time
from typing import Any

from .browser import BrowserGone, Session

MAX_RETRIES = 4


def request(
    session: Session,
    method: str,
    path: str,
    want_body: bool = True,
    json_body: dict[str, Any] | None = None,
    tolerate: tuple[int, ...] = (),
) -> dict[str, Any]:
    """A backend call with re-auth, backoff, and page recovery.

    Statuses in `tolerate` are returned to the caller instead of retried,
    so path probing can treat a 404 as "try the next candidate".
    """
    reauthed = False
    last_status: int | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        if session.page.is_closed():
            raise BrowserGone("The browser window was closed.")
        result = session.backend_fetch(
            method, path, want_body=want_body, json_body=json_body
        )
        status = result.get("status")
        last_status = status
        if status in (200, 204) or status in tolerate:
            return result
        if status in (401, 403) and not reauthed:
            reauthed = True
            session.clear_authorization()
            session.prime_authorization(force=True)
            continue
        if status == 429:
            retry_after = result.get("retry_after")
            if attempt == MAX_RETRIES:
                break
            try:
                delay = min(60.0, float(retry_after))
            except (TypeError, ValueError):
                delay = min(30.0, 2.0 ** attempt)
            time.sleep(delay)
            continue
        if status is None or (status and status >= 500):
            if attempt == MAX_RETRIES:
                break
            if status is None:
                session.recover_page()
            time.sleep(min(30.0, 2.0 ** attempt))
            continue
        break
    raise RuntimeError(f"{method} {path} failed (HTTP {last_status})")
