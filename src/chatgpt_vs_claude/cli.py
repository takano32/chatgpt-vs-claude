"""chatgpt-vs-claude: let a logged-in ChatGPT debate the local Claude Code."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from . import debate
from .browser import CHATGPT_ORIGIN, open_session
from .paths import (
    claude_workdir,
    default_profile_dir,
    default_transcript_dir,
    sibling_profiles,
)
from .transcript import Transcript


def _add_common_options(parser: argparse.ArgumentParser,
                        headless: bool = True) -> None:
    parser.add_argument(
        "--profile-dir", type=Path, default=argparse.SUPPRESS,
        help="Chromium profile directory (default: a dedicated one under "
             "~/.local/share/chatgpt-vs-claude)")
    parser.add_argument(
        "--browser-path", default=argparse.SUPPRESS,
        help="Chromium/Chrome executable to launch (default: Playwright's "
             "own, falling back to a system chromium)")
    if headless:
        parser.add_argument(
            "--headless", action="store_true", default=argparse.SUPPRESS,
            help="run the browser without a window (needs an existing "
                 "login; ChatGPT's bot check often blocks headless browsers)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chatgpt-vs-claude",
        description="Let a logged-in ChatGPT and the local Claude Code "
                    "debate each other, turn by turn, in this console.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser(
        "login",
        help="open the dedicated browser and sign in to ChatGPT (once)")
    login.add_argument(
        "--copy-from", type=Path, default=None,
        help="seed the profile from a sibling tool's profile directory "
             "(e.g. chatgpt-memory-distiller) to reuse its login")
    _add_common_options(login)

    status = commands.add_parser(
        "status", help="check whether the ChatGPT login is still valid "
                       "(always headless)")
    _add_common_options(status, headless=False)

    discuss = commands.add_parser(
        "discuss", help="run an automatic debate on a topic")
    discuss.add_argument("topic", help="the debate topic")
    discuss.add_argument(
        "--turns", type=int, default=3,
        help="statements per side (default: 3)")
    discuss.add_argument(
        "--first", choices=("chatgpt", "claude"), default="chatgpt",
        help="who opens the debate (default: chatgpt)")
    discuss.add_argument(
        "--max-chars", type=int, default=400,
        help="suggested length per statement (default: 400)")
    discuss.add_argument(
        "--instructions", default=None,
        help="extra rule appended for both sides "
             "(e.g. '英語で議論する')")
    discuss.add_argument(
        "--chatgpt-model", default=None,
        help="ChatGPT model slug for the ?model= URL (default: the "
             "account's default)")
    discuss.add_argument(
        "--claude-model", default=None,
        help="model passed to `claude --model` (default: the CLI's default)")
    discuss.add_argument(
        "--claude-command", default="claude",
        help="the Claude Code CLI to run (default: claude)")
    discuss.add_argument(
        "--timeout", type=int, default=600,
        help="seconds to wait for each ChatGPT reply (default: 600)")
    discuss.add_argument(
        "--claude-timeout", type=int, default=600,
        help="seconds to wait for each Claude reply (default: 600)")
    discuss.add_argument(
        "--transcript-dir", type=Path, default=argparse.SUPPRESS,
        help="where to write the transcript "
             f"(default: ./{default_transcript_dir().name}/)")
    _add_common_options(discuss)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.headless = getattr(args, "headless", False)
    args.browser_path = getattr(args, "browser_path", None)

    try:
        # Resolving the defaults touches the working directory, which can
        # have been deleted; that must read as an error, not a traceback.
        profile_dir = getattr(args, "profile_dir", None) \
            or default_profile_dir()
        if args.command == "login":
            return cmd_login(args, profile_dir)
        if args.command == "status":
            return cmd_status(args, profile_dir)
        if args.command == "discuss":
            transcript_dir = getattr(args, "transcript_dir", None) \
                or default_transcript_dir()
            _warn_if_exposed(transcript_dir)
            return cmd_discuss(args, profile_dir, transcript_dir)
        raise RuntimeError(f"Unknown command: {args.command}")
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except PlaywrightError as error:
        print(f"browser error: {error}", file=sys.stderr)
        print("The transcript written so far is safe in the transcript "
              "directory.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. The transcript written so far is safe in the "
              "transcript directory.", file=sys.stderr)
        return 130


def _warn_if_exposed(directory: Path) -> None:
    """Warn before writing transcripts into a repository that tracks them.

    The default is relative to the working directory, so the tool can end
    up writing conversation content into someone else's checkout. This repo
    ignores the directory name; another one has no reason to.
    """
    anchor = directory
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=anchor, capture_output=True, text=True, timeout=5,
        )
        if inside.returncode != 0:
            # git could not answer (missing binary, dubious ownership, a
            # broken repo). "Not a repository" is the only silent case.
            if "not a git repository" in (inside.stderr or "").lower():
                return
            raise OSError(inside.stderr or "git rev-parse failed")
        if inside.stdout.strip() != "true":
            return
        # The trailing slash lets a directory-only pattern match a
        # directory that does not exist yet — which is exactly when the
        # warning is useful, before anything has been written.
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", f"{directory}/"],
            cwd=anchor, capture_output=True, timeout=5,
        )
        if ignored.returncode == 0:
            return
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=anchor, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"WARNING: could not check whether {directory} is ignored by "
              f"git ({error}). It holds your conversations verbatim — make "
              "sure it is not committed.", file=sys.stderr)
        return

    pattern = f"{directory.name}/"
    if root.returncode == 0 and root.stdout.strip():
        try:
            relative = directory.resolve().relative_to(root.stdout.strip())
            pattern = f"/{relative}/"
        except ValueError:
            pass
    print(f"WARNING: {directory} holds your conversations verbatim and sits "
          f"in a git repository that does not ignore it. Add '{pattern}' to "
          ".gitignore, or pass --transcript-dir to write somewhere else.",
          file=sys.stderr)


def _copy_profile(source: Path, target: Path) -> None:
    source = source.expanduser()
    if not (source / "Default").exists():
        raise RuntimeError(
            f"{source} does not look like a Chromium profile "
            "(no Default/ inside).")
    if target.exists() and any(target.iterdir()):
        raise RuntimeError(
            f"{target} already exists. Remove it first, or run login "
            "without --copy-from to sign in there.")
    if (source / "SingletonLock").exists():
        print("WARNING: the source profile looks in use (SingletonLock "
              "present). Close that browser first, or the copy may be "
              "inconsistent.", file=sys.stderr)
    print(f"Copying profile {source} -> {target} ...")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Singleton* are stale liveness markers (symlinks on Linux); copying
    # them would make the new profile look locked or in use.
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("Singleton*"))
    target.chmod(0o700)


def cmd_login(args: argparse.Namespace, profile_dir: Path) -> int:
    if args.copy_from is not None:
        _copy_profile(args.copy_from, profile_dir)
    elif not (profile_dir / "Default").exists():
        siblings = sibling_profiles()
        if siblings:
            print(f"note: an existing ChatGPT login was found at "
                  f"{siblings[0]}. Reuse it without signing in again:\n"
                  f"  chatgpt-vs-claude login --copy-from {siblings[0]}",
                  file=sys.stderr)
    print(f"Opening a dedicated browser (profile: {profile_dir}) ...")
    print("Sign in to ChatGPT in that window. This tool waits up to 10 minutes.")
    with open_session(profile_dir, headless=args.headless,
                      browser_path=args.browser_path) as session:
        session.page.goto(CHATGPT_ORIGIN, wait_until="domcontentloaded")
        if not session.wait_for_login():
            print("error: no login detected within the timeout",
                  file=sys.stderr)
            return 1
        print("Login detected; waiting for the session to be saved "
              "(finish any remaining sign-in steps in the window) ...")
        if not session.wait_for_session_cookie():
            print("error: no persistent session cookie appeared, so the "
                  "login would not survive closing the browser. Complete "
                  "every sign-in step, then run `login` again.",
                  file=sys.stderr)
            return 1
        # Let the app finish its post-login requests before closing.
        session.page.wait_for_timeout(5000)
        print("Login saved. You can now run "
              "`chatgpt-vs-claude discuss \"<topic>\"`.")
        return 0


def cmd_status(args: argparse.Namespace, profile_dir: Path) -> int:
    if not (profile_dir / "Default").exists():
        print(f"Not logged in: no profile at {profile_dir}.")
        _print_login_hint()
        return 1
    with open_session(profile_dir, headless=True,
                      browser_path=args.browser_path) as session:
        state = session.login_state()
        has_cookie = session.has_session_cookie()
    if state == "logged_in":
        print(f"Logged in (profile: {profile_dir}).")
        return 0
    if state == "unknown" and has_cookie:
        # The cookie alone does not prove the session still works, and
        # claiming "logged in" here could send someone into a debate that
        # fails at the first message. Say what is actually known.
        print("A session cookie exists, but the login could not be "
              "verified (the session endpoint did not answer — possibly "
              "the bot check). `discuss` will show whether it still works.")
        return 2
    print("Not logged in: the session has expired."
          if has_cookie or state == "signed_out"
          else f"Not logged in (profile: {profile_dir}).")
    _print_login_hint()
    return 1


def _print_login_hint() -> None:
    siblings = sibling_profiles()
    if siblings:
        print(f"Run `chatgpt-vs-claude login --copy-from {siblings[0]}` to "
              "reuse an existing login, or `chatgpt-vs-claude login` to "
              "sign in.")
    else:
        print("Run `chatgpt-vs-claude login` to sign in.")


def _require_login(session) -> None:
    if session.is_logged_in():
        return
    message = ("Not logged in to ChatGPT. "
               "Run `chatgpt-vs-claude login` first.")
    siblings = sibling_profiles()
    if siblings:
        message += (f" An existing login was found at {siblings[0]} — reuse "
                    f"it with `chatgpt-vs-claude login --copy-from "
                    f"{siblings[0]}`.")
    raise RuntimeError(message)


STATEMENT_COLORS = {
    debate.CHATGPT_NAME: "32",  # green
    debate.CLAUDE_NAME: "33",   # yellow/orange
}


def _print_statement(name: str, index: int, total: int, text: str) -> None:
    header = f"── {name} ({index}/{total}) "
    line = header + "─" * max(0, 60 - len(header))
    if sys.stdout.isatty():
        color = STATEMENT_COLORS.get(name, "0")
        line = f"\x1b[1;{color}m{line}\x1b[0m"
    print()
    print(line)
    print(text.strip())
    sys.stdout.flush()


def cmd_discuss(args: argparse.Namespace, profile_dir: Path,
                transcript_dir: Path) -> int:
    if args.turns < 1:
        raise RuntimeError("--turns must be at least 1")
    # Fail before ChatGPT speaks: with the default order the first Claude
    # call happens only after a real conversation already exists on the
    # user's account, which is too late to learn the command is missing.
    if shutil.which(args.claude_command) is None:
        raise RuntimeError(
            f"`{args.claude_command}` was not found. Install Claude Code "
            "(https://claude.com/claude-code) or point --claude-command at "
            "it. Nothing was sent to ChatGPT.")
    settings = {
        "turns": args.turns,
        "first": args.first,
        "max_chars": args.max_chars,
    }
    if args.instructions:
        settings["instructions"] = args.instructions
    if args.chatgpt_model:
        settings["chatgpt_model"] = args.chatgpt_model
    if args.claude_model:
        settings["claude_model"] = args.claude_model

    with open_session(profile_dir, headless=args.headless,
                      browser_path=args.browser_path) as session:
        _require_login(session)
        transcript = Transcript(transcript_dir, args.topic, settings)
        print(f"Transcript: {transcript.md_path}")

        chatgpt_speaker = debate.ChatGPTSpeaker(
            session,
            model=args.chatgpt_model,
            timeout_seconds=args.timeout,
            report=lambda text: print(text, file=sys.stderr),
        )
        claude_speaker = debate.ClaudeSpeaker(
            model=args.claude_model,
            command=args.claude_command,
            timeout_seconds=args.claude_timeout,
            cwd=claude_workdir(),
        )
        if args.first == "chatgpt":
            first, second = chatgpt_speaker, claude_speaker
        else:
            first, second = claude_speaker, chatgpt_speaker

        def record_links() -> None:
            # Record the pointers as soon as they exist, so even an
            # interrupted run's transcript says where to find both sides.
            if chatgpt_speaker.conversation_url:
                transcript.set_link("chatgpt", chatgpt_speaker.conversation_url)
            if claude_speaker.session_id:
                transcript.set_link("claude_session", claude_speaker.session_id)

        def on_statement(name: str, index: int, total: int,
                         text: str) -> None:
            _print_statement(name, index, total, text)
            record_links()

        try:
            debate.run_debate(
                first=first,
                second=second,
                topic=args.topic,
                turns=args.turns,
                max_chars=args.max_chars,
                instructions=args.instructions,
                transcript=transcript,
                on_statement=on_statement,
            )
        finally:
            # A timeout or interrupt can strike after a message reached
            # ChatGPT but before its statement completed; the conversation
            # exists then, and the transcript must still point at it.
            record_links()

    print()
    print(f"Done: {args.turns} statements each.")
    if chatgpt_speaker.conversation_url:
        print(f"ChatGPT conversation: {chatgpt_speaker.conversation_url}")
    print(f"Transcript: {transcript.md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
