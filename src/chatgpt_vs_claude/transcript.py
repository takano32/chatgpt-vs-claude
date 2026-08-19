"""Write the debate to disk as it happens (JSON + Markdown).

Both files are rewritten atomically after every statement, so an
interrupted run keeps everything said so far. Files are 0600 in a 0700
directory: a transcript is a conversation held on the user's accounts and
should not leak through a world-readable file or an unaware git commit.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def render_markdown(data: dict[str, Any]) -> str:
    lines = [f"# ChatGPT vs Claude — {data['topic']}", ""]
    lines.append(f"- 開始: {data['started_at']}")
    for key, value in (data.get("settings") or {}).items():
        lines.append(f"- {key}: {value}")
    for key, value in (data.get("links") or {}).items():
        lines.append(f"- {key}: {value}")
    for turn in data.get("turns", []):
        lines.append("")
        lines.append(f"## {turn['speaker']} ({turn['index']}/{turn['total']})")
        lines.append("")
        lines.append(turn["text"])
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    # 0600 from the very first byte: chmod-after-write would leave the
    # whole conversation world-readable for a moment (and permanently, if
    # interrupted between the two calls).
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    temp_path.replace(path)


class Transcript:
    def __init__(self, directory: Path, topic: str,
                 settings: dict[str, Any] | None = None):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base = f"debate-{stamp}"
        suffix = ""
        counter = 1
        while (directory / f"{base}{suffix}.json").exists():
            counter += 1
            suffix = f"-{counter}"
        self.json_path = directory / f"{base}{suffix}.json"
        self.md_path = directory / f"{base}{suffix}.md"
        self.data: dict[str, Any] = {
            "topic": topic,
            "started_at": utc_now(),
            "settings": settings or {},
            "links": {},
            "turns": [],
        }
        self._write()

    def set_link(self, key: str, value: str) -> None:
        """Record a pointer (conversation URL, session id) once known."""
        if self.data["links"].get(key) == value:
            return
        self.data["links"][key] = value
        self._write()

    def add(self, speaker: str, index: int, total: int, text: str) -> None:
        self.data["turns"].append({
            "speaker": speaker,
            "index": index,
            "total": total,
            "at": utc_now(),
            "text": text,
        })
        self._write()

    def _write(self) -> None:
        _atomic_write(self.json_path,
                      json.dumps(self.data, ensure_ascii=False, indent=2))
        _atomic_write(self.md_path, render_markdown(self.data))
