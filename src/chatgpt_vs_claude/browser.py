"""Isolated Chromium session driven by Playwright.

Everything runs in a dedicated persistent profile so the user's normal
browser is never touched. All backend-api calls are executed inside the
authenticated page context; the short-lived Authorization header is held in
process memory only and is never logged or written to disk.
"""

from __future__ import annotations

import json
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
)
from playwright.sync_api import sync_playwright

CHATGPT_ORIGIN = "https://chatgpt.com"
SESSION_URL = f"{CHATGPT_ORIGIN}/api/auth/session"
SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "__Secure-next-auth.session-token.0",
)
SYSTEM_CHROMIUM_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
)


class BrowserGone(RuntimeError):
    """The browser session died; a fresh one can continue the work."""


def resolve_browser_path(explicit: str | None) -> str | None:
    """Return an explicit or system Chromium path, or None for Playwright's own."""
    if explicit:
        return explicit
    return None


def find_system_chromium() -> str | None:
    for name in SYSTEM_CHROMIUM_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


class Session:
    def __init__(self, context: BrowserContext, page: Page):
        self.context = context
        self.page = page
        # Where to park the page when its context dies.
        self.home_url = f"{CHATGPT_ORIGIN}/"
        self._authorization: str | None = None

    # -- auth --------------------------------------------------------------

    def has_session_cookie(self) -> bool:
        try:
            cookies = self.context.cookies(CHATGPT_ORIGIN)
        except PlaywrightError:
            return False
        return any(
            cookie.get("name") in SESSION_COOKIE_NAMES for cookie in cookies
        )

    def login_state(self) -> str:
        """'logged_in', 'signed_out', or 'unknown' from the session endpoint.

        The session endpoint is requested by absolute URL through the
        context's cookie jar: during the login flow the visible page sits on
        auth.openai.com, and a relative fetch would hit that origin's own
        endpoint and report a false positive before the redirect back to
        chatgpt.com has minted a session. 'unknown' means the endpoint was
        unreachable or bot-blocked, which says nothing about the session.
        """
        try:
            response = self.context.request.get(SESSION_URL, timeout=20000)
            if response.ok:
                data = response.json()
                if isinstance(data, dict) and (
                    data.get("accessToken") or data.get("user")
                ):
                    return "logged_in"
                # A well-formed but empty session means signed out.
                if isinstance(data, dict) and "WARNING_BANNER" in data:
                    return "signed_out"
        except Exception:
            pass
        return "unknown"

    def is_logged_in(self) -> bool:
        state = self.login_state()
        if state == "logged_in":
            return True
        if state == "signed_out":
            return False
        # The endpoint can be unreachable or bot-blocked from the driver
        # process; fall back to the persisted session cookie.
        return self.has_session_cookie()

    def wait_for_login(self, timeout_seconds: int = 600) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.page.is_closed():
                raise RuntimeError(
                    "The browser window was closed before a login was detected."
                )
            if self.is_logged_in():
                return True
            time.sleep(3)
        return False

    def wait_for_session_cookie(self, timeout_seconds: int = 180) -> bool:
        """Wait until the persistent session cookie exists.

        is_logged_in() can flip to true mid-OAuth, before the session
        cookie is minted; a browser closed at that moment loses the login.
        Only the cookie survives a restart, so closing must wait for it.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.page.is_closed():
                return self.has_session_cookie()
            if self.has_session_cookie():
                return True
            time.sleep(2)
        return False

    def prime_authorization(self, force: bool = False) -> None:
        """Capture the app's own Authorization header during a page reload.

        The token stays in this process's memory only. Raw bearer extraction
        via /api/auth/session is deliberately avoided: requests outside the
        app's authenticated layer can be rejected.
        """
        if self._authorization and not force:
            return
        self._authorization = None
        try:
            with self.page.expect_request(
                lambda request: "/backend-api/" in request.url
                and (request.headers.get("authorization") or "").startswith("Bearer "),
                timeout=30000,
            ) as request_info:
                self.page.reload(wait_until="domcontentloaded")
            self._authorization = request_info.value.headers["authorization"]
        except PlaywrightError as error:
            raise RuntimeError(
                "Could not capture an authenticated backend-api request "
                f"({type(error).__name__}). Make sure you are logged in "
                "(chatgpt-vs-claude login) and the browser window "
                "stays open."
            ) from error

    def clear_authorization(self) -> None:
        self._authorization = None

    # -- in-page HTTP ------------------------------------------------------

    def backend_fetch(
        self,
        method: str,
        path: str,
        want_body: bool = True,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run fetch() inside the page against a /backend-api/ path.

        Returns {status, retry_after, body(text or None)}.
        """
        if self._authorization is None:
            self.prime_authorization()
        try:
            result = self._evaluate_fetch(method, path, want_body, json_body)
        except PlaywrightError as error:
            # The app navigates on its own, which can destroy the execution
            # context mid-call. Park the page somewhere stable and try once
            # more before giving up.
            try:
                self.recover_page()
                result = self._evaluate_fetch(method, path, want_body, json_body)
            except PlaywrightError:
                return {"status": None, "retry_after": None, "body": None,
                        "error": str(error)}
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected in-page fetch result")
        return result

    def recover_page(self) -> None:
        """Return the page to a stable authenticated document."""
        if self.page.is_closed():
            raise BrowserGone("The browser window was closed.")
        try:
            self.page.goto(self.home_url, wait_until="domcontentloaded",
                           timeout=60000)
        except PlaywrightError as error:
            raise BrowserGone(f"The browser session ended: {error}") from error
        self.page.wait_for_timeout(1500)

    def _evaluate_fetch(
        self, method: str, path: str, want_body: bool,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        return self.page.evaluate(
            """async ({method, path, auth, wantBody, jsonBody}) => {
              try {
                const headers = {authorization: auth};
                if (jsonBody) headers['content-type'] = 'application/json';
                const response = await fetch(path, {
                  method,
                  credentials: 'include',
                  headers,
                  body: jsonBody ? JSON.stringify(jsonBody) : undefined,
                });
                let body = null;
                if (wantBody) {
                  try { body = await response.text(); } catch { body = null; }
                }
                return {
                  status: response.status,
                  retry_after: response.headers.get('retry-after'),
                  body,
                };
              } catch (error) {
                return {status: null, retry_after: null, body: null,
                        error: String(error)};
              }
            }""",
            {
                "method": method,
                "path": path,
                "auth": self._authorization,
                "wantBody": want_body,
                "jsonBody": json_body,
            },
        )


def mark_profile_clean(profile_dir: Path) -> None:
    """Clear the 'did not shut down cleanly' flags in the profile.

    Playwright terminates Chromium in a way that leaves exit_type=Crashed,
    which makes the next launch show a 'Restore pages?' bubble that can steal
    focus during login.
    """
    preferences_path = profile_dir / "Default" / "Preferences"
    if not preferences_path.exists():
        return
    try:
        preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    profile = preferences.get("profile")
    if not isinstance(profile, dict):
        return
    if profile.get("exit_type") == "Normal" and profile.get("exited_cleanly"):
        return
    profile["exit_type"] = "Normal"
    profile["exited_cleanly"] = True
    try:
        preferences_path.write_text(
            json.dumps(preferences), encoding="utf-8"
        )
    except OSError:
        pass


@contextmanager
def open_session(
    profile_dir: Path,
    headless: bool = False,
    browser_path: str | None = None,
) -> Iterator[Session]:
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    mark_profile_clean(profile_dir)
    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "no_viewport": True,  # pages follow the real window size
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--hide-crash-restore-bubble",
            ],
        }
        executable = resolve_browser_path(browser_path)
        if executable:
            launch_kwargs["executable_path"] = executable
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir), **launch_kwargs
            )
        except PlaywrightError as error:
            # Fall back to a system Chromium only when Playwright's bundled
            # browser is missing; any other launch failure (locked or corrupt
            # profile, crash) must surface as-is.
            if executable or "executable doesn't exist" not in str(error).lower():
                raise
            fallback = find_system_chromium()
            if not fallback:
                raise RuntimeError(
                    "No Chromium available. Run `playwright install chromium` "
                    "or install a system chromium package."
                ) from error
            launch_kwargs["executable_path"] = fallback
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir), **launch_kwargs
            )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield Session(context, page)
        finally:
            context.close()
