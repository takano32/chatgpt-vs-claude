"""Talk to Claude through the local Claude Code CLI.

The debate holds one long-lived `claude -p` process with stream-json on
both ends (ClaudeProcess): starting the CLI costs ~9 seconds, so paying it
once instead of per statement cuts each Claude turn to the model call
alone (measured 9.3s -> 1.7s). The CLI uses the machine's existing Claude
Code login, so no API key is needed. The working directory is pinned by
the caller because the CLI keys its session store to the cwd: resuming
from a different directory would not find the session.

ask() remains as the single-shot form (`claude -p` per call, `--resume`
for continuity) for callers that only need one exchange.
"""

from __future__ import annotations

import collections
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, IO

DEFAULT_COMMAND = "claude"


class ClaudeError(RuntimeError):
    """The claude CLI failed or answered something unusable."""


class _ProcessGone(Exception):
    """The long-lived process died; a respawn can resume the session."""


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


def _pump_lines(stream: IO[str], sink: queue.Queue) -> None:
    for line in iter(stream.readline, ""):
        sink.put(line)
    sink.put(None)


def _pump_tail(stream: IO[str], sink: collections.deque) -> None:
    for line in iter(stream.readline, ""):
        sink.append(line.rstrip())


class ClaudeProcess:
    """One `claude` process holding the whole conversation.

    User messages go in as stream-json lines; each turn finishes with a
    `result` event carrying the same fields as the one-shot json output.
    If the process dies mid-debate, one respawn resumes the recorded
    session before the failure is surfaced.
    """

    def __init__(self, model: str | None = None,
                 command: str = DEFAULT_COMMAND,
                 cwd: Path | None = None,
                 system_prompt: str | None = None,
                 timeout_seconds: int = 600):
        self.model = model
        self.command = command
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.timeout_seconds = timeout_seconds
        self.session_id: str | None = None
        self._proc: subprocess.Popen | None = None
        self._events: queue.Queue | None = None
        self._stderr_tail: collections.deque = collections.deque(maxlen=50)

    def _spawn(self) -> None:
        self._kill()
        argv = [self.command, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose"]
        if self.session_id:
            argv += ["--resume", self.session_id]
        if self.model:
            argv += ["--model", self.model]
        if self.system_prompt:
            argv += ["--append-system-prompt", self.system_prompt]
        if self.cwd is not None:
            self.cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(self.cwd) if self.cwd is not None else None,
            )
        except FileNotFoundError:
            raise ClaudeError(
                f"`{self.command}` was not found. Install Claude Code "
                "(https://claude.com/claude-code) or point --claude-command "
                "at it.") from None
        self._events = queue.Queue()
        self._stderr_tail = collections.deque(maxlen=50)
        threading.Thread(target=_pump_lines,
                         args=(self._proc.stdout, self._events),
                         daemon=True).start()
        # stderr must be drained or a chatty CLI blocks on a full pipe.
        threading.Thread(target=_pump_tail,
                         args=(self._proc.stderr, self._stderr_tail),
                         daemon=True).start()

    def ask(self, prompt: str) -> str:
        if self._proc is None or self._proc.poll() is not None:
            self._spawn()
        try:
            return self._ask_once(prompt)
        except _ProcessGone:
            # One respawn resumes the session; a second death is real.
            self._spawn()
            try:
                return self._ask_once(prompt)
            except _ProcessGone as error:
                raise ClaudeError(
                    f"`{self.command}` keeps exiting mid-conversation: "
                    f"{error}") from None

    def _ask_once(self, prompt: str) -> str:
        assert self._proc is not None and self._events is not None
        message = {"type": "user",
                   "message": {"role": "user",
                               "content": [{"type": "text",
                                            "text": prompt}]}}
        try:
            self._proc.stdin.write(
                json.dumps(message, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            raise _ProcessGone(self._stderr_note(error)) from None
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill()
                raise ClaudeError(
                    f"`{self.command}` did not answer within "
                    f"{self.timeout_seconds}s (--claude-timeout raises "
                    "the limit).")
            try:
                line = self._events.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                raise _ProcessGone(self._stderr_note("stdout closed"))
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
            result = event.get("result")
            if event.get("is_error"):
                raise ClaudeError(
                    "claude reported an error: "
                    + _tail(result if isinstance(result, str)
                            else json.dumps(event)))
            if not isinstance(result, str) or not result.strip():
                raise ClaudeError("claude returned an empty reply")
            return result.strip()

    def _stderr_note(self, error: Any) -> str:
        tail = "\n".join(list(self._stderr_tail)[-5:])
        return f"{error}" + (f" | stderr: {_tail(tail)}" if tail else "")

    def _kill(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.kill()
            self._proc.wait(timeout=5)
        except Exception:
            pass
        self._proc = None

    def close(self) -> None:
        """End the conversation and let the process exit cleanly."""
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=10)
        except Exception:
            self._kill()
        self._proc = None
