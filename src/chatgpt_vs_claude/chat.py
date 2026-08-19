"""Drive the real ChatGPT UI to send prompts and read the replies.

POST /backend-api/conversation is gated by proof-of-work sentinels, so
reimplementing it is fragile. Instead each prompt is typed into the app's
own composer — the app handles all of that itself — and the finished reply
is read back through the conversation API, whose GETs are not gated.

A debate needs follow-up messages inside one conversation, which the
sibling tools never do. A follow-up cannot wait for a URL change the way
the first message does, so the reply watermark is time-based: the caller
passes the create_time of the last reply it consumed, and only a finished
assistant message newer than that counts as the answer.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError

from . import api
from .browser import CHATGPT_ORIGIN, Session

COMPOSER_SELECTOR = "#prompt-textarea"
SEND_BUTTON_SELECTOR = '[data-testid="send-button"]'
STOP_BUTTON_SELECTOR = '[data-testid="stop-button"]'
CONVERSATION_PATH = "/backend-api/conversation/{conversation_id}"
CONVERSATION_ID_PATTERN = re.compile(r"/c/([0-9a-fA-F-]{8,})")


def conversation_id_from_url(url: str) -> str | None:
    match = CONVERSATION_ID_PATTERN.search(url)
    return match.group(1) if match else None


def conversation_url(conversation_id: str) -> str:
    return f"{CHATGPT_ORIGIN}/c/{conversation_id}"


def start_new_chat(session: Session, model: str | None = None) -> None:
    url = f"{CHATGPT_ORIGIN}/"
    if model:
        url += f"?model={model}"
    session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
    session.page.wait_for_timeout(2000)


def _fill_and_submit(session: Session, text: str) -> None:
    page = session.page
    composer = page.locator(COMPOSER_SELECTOR)
    try:
        composer.wait_for(state="visible", timeout=30000)
    except PlaywrightError as error:
        title = ""
        try:
            title = page.title()
        except PlaywrightError:
            pass
        if "just a moment" in title.lower():
            # Cloudflare's bot check. A valid session cookie does not help;
            # headless browsers in particular are stopped here.
            raise RuntimeError(
                "ChatGPT is stuck on a bot check ('Just a moment...'). "
                "Headless mode is usually blocked there — run without "
                "--headless so the check can pass in a real window."
            ) from error
        raise
    composer.click()
    composer.fill(text)
    page.wait_for_timeout(500)
    send_button = page.locator(SEND_BUTTON_SELECTOR)
    try:
        send_button.click(timeout=5000)
    except PlaywrightError:
        # The send button's testid has changed before; Enter also submits.
        composer.press("Enter")


def send_message(session: Session, text: str,
                 timeout_seconds: int = 120) -> str:
    """Type into the composer, send, and return the new conversation id."""
    page = session.page
    _fill_and_submit(session, text)
    try:
        page.wait_for_url(re.compile(r"/c/[0-9a-fA-F-]{8,}"),
                          timeout=timeout_seconds * 1000)
    except PlaywrightError as error:
        raise RuntimeError(
            "The message did not turn into a conversation (no /c/<id> URL). "
            "Check the browser window for blockers such as a verification "
            "screen or an onboarding dialog."
        ) from error
    conversation_id = conversation_id_from_url(page.url)
    if not conversation_id:
        raise RuntimeError("Could not read the conversation id from the URL")
    return conversation_id


def wait_for_composer_idle(session: Session,
                           timeout_seconds: int = 30) -> None:
    """Wait until the previous reply has stopped streaming in the UI.

    The conversation API can report a reply finished while the UI is still
    painting it; during that window the send button is a stop button and a
    submit would cut the reply short. Best-effort: after the timeout the
    caller proceeds anyway and the submit fallbacks take over.
    """
    page = session.page
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            stop = page.locator(STOP_BUTTON_SELECTOR)
            if stop.count() == 0 or not stop.first.is_visible():
                return
        except PlaywrightError:
            return
        time.sleep(1.0)


def _normalized(text: str) -> str:
    """Whitespace-normalized form for comparing composer content.

    The composer is a rich-text editor that re-serializes pasted text into
    paragraphs, so inner_text() never equals the filled string verbatim —
    newline counts differ and NBSPs appear. Only the words can be compared.
    """
    return " ".join(text.split())


def send_followup(session: Session, conversation_id: str, text: str) -> None:
    """Send a message into an existing conversation.

    There is no URL change to confirm the send, so the only in-page signal
    is the composer clearing. If the text is still sitting there after the
    submit, one Enter retries it — pressing Enter on an already-empty
    composer is the case that must be avoided, since it could submit a
    stray empty message, so the retry is gated on the text still being
    present. If even the retry leaves the text in place, something is
    blocking the UI and waiting for a reply would just burn the timeout.
    """
    page = session.page
    if conversation_id_from_url(page.url) != conversation_id:
        page.goto(conversation_url(conversation_id),
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
    wait_for_composer_idle(session)
    _fill_and_submit(session, text)
    page.wait_for_timeout(1500)
    try:
        composer = page.locator(COMPOSER_SELECTOR)
        leftover = _normalized(composer.inner_text(timeout=5000))
        if leftover and leftover == _normalized(text):
            composer.press("Enter")
            page.wait_for_timeout(1500)
            leftover = _normalized(composer.inner_text(timeout=5000))
            if leftover and leftover == _normalized(text):
                raise RuntimeError(
                    "The follow-up message stayed in the composer after two "
                    "submit attempts. Check the browser window for blockers "
                    "such as a verification screen or a message-cap dialog."
                )
    except PlaywrightError:
        pass


def latest_assistant_text(
    conversation: Any,
    after_time: float = float("-inf"),
) -> tuple[str, bool, float]:
    """The assistant's newest reply newer than `after_time`.

    Returns (text, finished, create_time-watermark). A reply can be split
    across several messages — continuation chunks carry end_turn=False
    before the turn-ending one — and the relayed statement must be the
    whole turn, so the text joins the newest message with the unbroken run
    of continuation chunks right before it. Two complete messages stay
    separate turns: only the newest is the reply.
    """
    if not isinstance(conversation, dict):
        return "", False, float("-inf")
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        return "", False, float("-inf")
    replies: list[dict[str, Any]] = []
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        if not isinstance(author, dict) or author.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, dict):
            continue
        # Reasoning models interleave "thoughts" and tool messages; only the
        # plain text message carries the reply.
        if content.get("content_type") != "text":
            continue
        # Tool calls (e.g. bio/memory saves) are assistant "text" messages
        # too, addressed to the tool instead of the user, and they finish
        # mid-turn. Mistaking one for the reply would end the wait early.
        if (message.get("recipient") or "all") != "all":
            continue
        parts = [part for part in (content.get("parts") or [])
                 if isinstance(part, str)]
        text = "\n".join(parts).strip()
        if not text:
            continue
        create_time = message.get("create_time")
        if not isinstance(create_time, (int, float)):
            create_time = 0.0
        if create_time <= after_time:
            continue
        replies.append({"message": message, "text": text,
                        "time": create_time})
    if not replies:
        return "", False, float("-inf")
    replies.sort(key=lambda reply: reply["time"])
    best = replies[-1]["message"]
    best_time = replies[-1]["time"]
    chain = [replies[-1]]
    for reply in reversed(replies[:-1]):
        if reply["message"].get("end_turn") is False:
            chain.insert(0, reply)
        else:
            break
    text = "\n\n".join(reply["text"] for reply in chain)
    # end_turn=False marks a chunk the model will follow up on; only a
    # turn-ending message is final. Exception: a reply cut by the token cap
    # carries end_turn=False but nothing ever follows — waiting on it would
    # just burn the timeout.
    metadata = best.get("metadata")
    finish_type = None
    if isinstance(metadata, dict):
        details = metadata.get("finish_details")
        if isinstance(details, dict):
            finish_type = details.get("type")
    finished = (best.get("status") == "finished_successfully"
                and (best.get("end_turn") is not False
                     or finish_type == "max_tokens"))
    return text, finished, best_time


def wait_for_reply(
    session: Session,
    conversation_id: str,
    after_time: float = float("-inf"),
    timeout_seconds: int = 600,
    poll_seconds: float = 3.0,
    report: Callable[[str], None] | None = None,
) -> tuple[str, float]:
    """Poll the conversation until a reply newer than `after_time` finishes.

    Returns (text, create_time); the create_time is the watermark to pass
    back in for the next follow-up's wait.
    """
    deadline = time.monotonic() + timeout_seconds
    path = CONVERSATION_PATH.format(conversation_id=conversation_id)
    last_text = ""
    while time.monotonic() < deadline:
        result = api.request(session, "GET", path, tolerate=(404,))
        if result.get("status") == 200 and result.get("body"):
            try:
                conversation = json.loads(result["body"])
            except json.JSONDecodeError:
                conversation = None
            text, finished, create_time = latest_assistant_text(
                conversation, after_time=after_time)
            if finished and text:
                return text, create_time
            if report and text and len(text) != len(last_text):
                report(f"  ... {len(text)} chars so far")
                last_text = text
        time.sleep(poll_seconds)
    raise RuntimeError(
        f"No finished reply within {timeout_seconds}s "
        f"(conversation {conversation_id}). The chat stays in the browser; "
        "re-run with a longer --timeout if it was still streaming."
    )
