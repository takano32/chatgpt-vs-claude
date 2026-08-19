"""Turn the two chat backends into one alternating debate.

The ChatGPT side speaks through the real UI (chat.py) because the send
endpoint is proof-of-work gated; the Claude side speaks through the local
`claude` CLI (claude_cli.py). This module decides who speaks when and what
each side is told; neither side ever talks to the other directly — every
statement is relayed verbatim inside a labeled envelope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from . import chat, claude_cli
from .browser import Session

CHATGPT_NAME = "ChatGPT"
CLAUDE_NAME = "Claude"

FINAL_NOTE = "これがあなたの最後の発言です。議論全体を踏まえて総括し、締めくくってください。"

# Claude Code's own system prompt frames the assistant as a coding tool, and
# some models refuse a non-technical debate outright ("職務範囲外").
# Appending this reframes the session: the user launched this app precisely
# to have Claude hold the discussion, so holding it IS the assigned task.
CLAUDE_SYSTEM_PROMPT = (
    "このセッションは、討論アプリ chatgpt-vs-claude の実行です。ユーザーは"
    "このアプリを自分で起動し、あなた (Claude) と ChatGPT による自動討論を"
    "明示的に依頼しています。このセッションでのあなたのタスクはコーディング"
    "作業ではなく、与えられたテーマの議論に Claude として参加することです。"
    "職務範囲外として断らず、通常の会話として率直に議論してください。"
)


def _rules(turns: int, max_chars: int, instructions: str | None) -> str:
    lines = [
        "ルール:",
        f"- あなたが発言できるのは合計 {turns} 回です。",
        f"- 1回の発言は {max_chars} 字以内を目安に、簡潔にまとめてください。",
        "- 相手の主張の要点を受け止めた上で、自分の立場と根拠を明確にしてください。",
        "- 挨拶・自己紹介・前置きは不要です。発言の内容だけを書いてください。",
    ]
    if instructions:
        lines.append(f"- {instructions}")
    return "\n".join(lines)


def _intro(opponent: str, topic: str, turns: int, max_chars: int,
           instructions: str | None) -> str:
    return (
        f"あなたはこれから {opponent} と1対1で議論します。"
        "このチャットはプログラムが自動で進行し、相手の発言はそのまま中継されます。\n\n"
        f"テーマ: {topic}\n\n"
        + _rules(turns, max_chars, instructions)
    )


def opening_prompt(topic: str, opponent: str, turns: int, max_chars: int,
                   instructions: str | None = None) -> str:
    return (
        _intro(opponent, topic, turns, max_chars, instructions)
        + "\n\nまず、このテーマに対するあなたの立場と主張を述べてください。"
    )


def opening_relay_prompt(topic: str, opponent: str, opponent_text: str,
                         turns: int, max_chars: int,
                         instructions: str | None = None,
                         final: bool = False) -> str:
    text = (
        _intro(opponent, topic, turns, max_chars, instructions)
        + f"\n\n{opponent} の最初の発言:\n---\n{opponent_text}\n---\n\n"
        "これに応答し、あなたの立場と主張を述べてください。"
    )
    if final:
        text += "\n" + FINAL_NOTE
    return text


def relay_prompt(opponent: str, opponent_text: str,
                 final: bool = False) -> str:
    text = (
        f"{opponent} の発言:\n---\n{opponent_text}\n---\n\n"
        "これに応答してください。"
    )
    if final:
        text += "\n" + FINAL_NOTE
    return text


class Speaker(Protocol):
    name: str

    def say(self, prompt: str) -> str: ...


class ChatGPTSpeaker:
    name = CHATGPT_NAME

    def __init__(self, session: Session, model: str | None = None,
                 timeout_seconds: int = 600,
                 report: Callable[[str], None] | None = None):
        self.session = session
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.report = report
        self.conversation_id: str | None = None
        self._last_reply_time = float("-inf")

    def say(self, prompt: str) -> str:
        if self.conversation_id is None:
            chat.start_new_chat(self.session, model=self.model)
            self.conversation_id = chat.send_message(self.session, prompt)
        else:
            chat.send_followup(self.session, self.conversation_id, prompt)
        text, reply_time = chat.wait_for_reply(
            self.session,
            self.conversation_id,
            after_time=self._last_reply_time,
            timeout_seconds=self.timeout_seconds,
            report=self.report,
        )
        self._last_reply_time = reply_time
        return text

    @property
    def conversation_url(self) -> str | None:
        if self.conversation_id is None:
            return None
        return chat.conversation_url(self.conversation_id)


class ClaudeSpeaker:
    name = CLAUDE_NAME

    def __init__(self, model: str | None = None,
                 command: str = claude_cli.DEFAULT_COMMAND,
                 timeout_seconds: int = 600,
                 cwd: Path | None = None,
                 system_prompt: str | None = CLAUDE_SYSTEM_PROMPT):
        self.model = model
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.session_id: str | None = None

    def say(self, prompt: str) -> str:
        reply, session_id = claude_cli.ask(
            prompt,
            session_id=self.session_id,
            model=self.model,
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            cwd=self.cwd,
            system_prompt=self.system_prompt,
        )
        self.session_id = session_id
        return reply


def run_debate(
    *,
    first: Speaker,
    second: Speaker,
    topic: str,
    turns: int,
    max_chars: int = 400,
    instructions: str | None = None,
    transcript=None,
    on_statement: Callable[[str, int, int, str], None] | None = None,
) -> list[dict[str, str]]:
    """Alternate statements between two speakers, `turns` rounds each.

    The transcript (when given) is appended to after every statement, so an
    interrupt keeps everything said so far. `on_statement(name, index,
    total, text)` fires after each statement for live display.
    """
    if turns < 1:
        raise ValueError("turns must be at least 1")
    statements: list[dict[str, str]] = []
    spoken = {first.name: 0, second.name: 0}
    last_text: str | None = None
    for round_index in range(turns):
        final = round_index == turns - 1
        for speaker, opponent in ((first, second), (second, first)):
            if last_text is None:
                prompt = opening_prompt(
                    topic, opponent.name, turns, max_chars, instructions)
            elif spoken[speaker.name] == 0:
                prompt = opening_relay_prompt(
                    topic, opponent.name, last_text, turns, max_chars,
                    instructions, final=final)
            else:
                prompt = relay_prompt(opponent.name, last_text, final=final)
            text = speaker.say(prompt)
            spoken[speaker.name] += 1
            statements.append({"speaker": speaker.name, "text": text})
            if transcript is not None:
                transcript.add(speaker.name, spoken[speaker.name], turns, text)
            if on_statement is not None:
                on_statement(speaker.name, spoken[speaker.name], turns, text)
            last_text = text
    return statements
