import pytest

from chatgpt_vs_claude.cli import build_parser, main


def test_discuss_defaults():
    args = build_parser().parse_args(["discuss", "AIと創造性"])
    assert args.topic == "AIと創造性"
    assert args.turns == 3
    assert args.first == "chatgpt"
    assert args.max_chars == 400
    assert args.claude_command == "claude"
    assert not hasattr(args, "transcript_dir")
    assert not hasattr(args, "profile_dir")


def test_discuss_requires_a_topic(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["discuss"])


def test_discuss_accepts_overrides(tmp_path):
    args = build_parser().parse_args([
        "discuss", "topic",
        "--turns", "5",
        "--first", "claude",
        "--chatgpt-model", "gpt-5-thinking",
        "--claude-model", "sonnet",
        "--transcript-dir", str(tmp_path),
        "--headless",
    ])
    assert args.turns == 5
    assert args.first == "claude"
    assert args.chatgpt_model == "gpt-5-thinking"
    assert args.claude_model == "sonnet"
    assert args.transcript_dir == tmp_path
    assert args.headless


def test_login_accepts_copy_from(tmp_path):
    args = build_parser().parse_args(
        ["login", "--copy-from", str(tmp_path)])
    assert args.copy_from == tmp_path


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_status_is_always_headless():
    # status hardcodes headless; offering the flag would make it dead.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["status", "--headless"])


def test_discuss_fails_fast_on_a_missing_claude_command(tmp_path, capsys):
    # The guard must fire before any browser opens or message is sent.
    exit_code = main([
        "discuss", "topic",
        "--claude-command", "/nonexistent/claude-xyz",
        "--transcript-dir", str(tmp_path),
    ])
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "not found" in stderr
    assert "Nothing was sent to ChatGPT" in stderr
