"""Talk to Claude through the local Claude Code CLI.

`claude -p` answers one prompt and exits; `--resume` with the session id
from the previous call continues that conversation with its context. The
CLI uses the machine's existing Claude Code login, so no API key is needed.
The working directory is pinned by the caller because the CLI keys its
session store to the cwd: resuming from a different directory would not
find the session.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DEFAULT_COMMAND = "claude"


class ClaudeError(RuntimeError):
    """The claude CLI failed or answered something unusable."""


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "… " + text[-limit:]


def parse_result(stdout: str) -> tuple[str, str | None]:
    """Extract (reply, session_id) from `--output-format json` output."""
    data = None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # Warnings can precede the JSON document; the object is one line.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    data = None
                break
    if not isinstance(data, dict):
        raise ClaudeError(
            "claude did not return the expected JSON output: "
            + _tail(stdout)
        )
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = None
    result = data.get("result")
    if data.get("is_error"):
        raise ClaudeError(
            "claude reported an error: "
            + _tail(result if isinstance(result, str) else json.dumps(data))
        )
    if not isinstance(result, str) or not result.strip():
        raise ClaudeError("claude returned an empty reply")
    return result.strip(), session_id


def ask(
    prompt: str,
    session_id: str | None = None,
    model: str | None = None,
    command: str = DEFAULT_COMMAND,
    timeout_seconds: int = 600,
    cwd: Path | None = None,
    system_prompt: str | None = None,
) -> tuple[str, str | None]:
    """One prompt in, one reply out. Returns (reply, session_id).

    The returned session id continues the conversation when passed back in;
    it falls back to the caller's id if the CLI stops reporting one. The
    system prompt is appended per invocation, so a resumed conversation
    needs it passed again every time.
    """
    argv = [command, "-p", "--output-format", "json"]
    if session_id:
        argv += ["--resume", session_id]
    if model:
        argv += ["--model", model]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    argv.append(prompt)
    if cwd is not None:
        cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd) if cwd is not None else None,
        )
    except FileNotFoundError:
        raise ClaudeError(
            f"`{command}` was not found. Install Claude Code "
            "(https://claude.com/claude-code) or point --claude-command at it."
        ) from None
    except subprocess.TimeoutExpired:
        raise ClaudeError(
            f"`{command}` did not answer within {timeout_seconds}s "
            "(--claude-timeout raises the limit)."
        ) from None
    if completed.returncode != 0:
        raise ClaudeError(
            f"`{command}` exited with {completed.returncode}: "
            + _tail(completed.stderr or completed.stdout)
        )
    reply, new_session_id = parse_result(completed.stdout)
    return reply, new_session_id or session_id
