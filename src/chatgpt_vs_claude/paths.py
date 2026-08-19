"""Filesystem locations, all isolated from the user's normal browser."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "chatgpt-vs-claude"

# Tools from the same family; their profiles hold a reusable ChatGPT login.
SIBLING_APPS = ("chatgpt-memory-distiller", "chatgpt-cleaner")


def _xdg_data_base() -> Path:
    configured = os.environ.get("XDG_DATA_HOME")
    return Path(configured).expanduser() if configured \
        else Path.home() / ".local/share"


def data_dir() -> Path:
    return _xdg_data_base() / APP_NAME


def default_profile_dir() -> Path:
    """Dedicated Chromium profile. Never points at a normal browser profile."""
    return data_dir() / "profile"


def claude_workdir() -> Path:
    """A stable, neutral working directory for the `claude` CLI.

    The CLI keys its session store to the working directory, so resuming a
    session requires the same cwd on every call. It also loads any CLAUDE.md
    it finds there, so this must not be the user's project directory: the
    debate would silently absorb unrelated project context.
    """
    return data_dir() / "claude"


TRANSCRIPT_DIR_NAME = "chatgpt-vs-claude-transcripts"


def default_transcript_dir() -> Path:
    """Transcripts land beside the working directory, not in $HOME.

    Keeping them next to the invocation makes them easy to find and to
    delete. They hold conversations from the user's accounts verbatim, so
    the repository ignores this name — never commit one.
    """
    return Path.cwd() / TRANSCRIPT_DIR_NAME


def sibling_profiles() -> list[Path]:
    """Profiles of sibling tools that already hold a ChatGPT login."""
    found = []
    for app in SIBLING_APPS:
        candidate = _xdg_data_base() / app / "profile"
        if (candidate / "Default" / "Cookies").exists():
            found.append(candidate)
    return found
