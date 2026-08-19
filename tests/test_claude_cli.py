import json
import os
import stat

import pytest

from chatgpt_vs_claude import claude_cli


def fake_claude(tmp_path, response, exit_code=0):
    """Create a fake `claude` executable that logs its argv."""
    log_path = tmp_path / "argv.log"
    script_path = tmp_path / "claude"
    script_path.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" >> "{log_path}"\n'
        f"cat <<'EOF'\n{response}\nEOF\n"
        f"exit {exit_code}\n"
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    return script_path, log_path


def result_json(text="hello", session_id="sess-1", is_error=False):
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": text,
        "session_id": session_id,
    })


def test_parse_result_reads_reply_and_session():
    reply, session_id = claude_cli.parse_result(result_json("こんにちは"))
    assert reply == "こんにちは"
    assert session_id == "sess-1"


def test_parse_result_skips_leading_noise():
    stdout = "some warning line\n" + result_json("ok")
    reply, session_id = claude_cli.parse_result(stdout)
    assert reply == "ok"
    assert session_id == "sess-1"


def test_parse_result_rejects_error_results():
    with pytest.raises(claude_cli.ClaudeError):
        claude_cli.parse_result(result_json("boom", is_error=True))


def test_parse_result_rejects_empty_replies():
    with pytest.raises(claude_cli.ClaudeError):
        claude_cli.parse_result(result_json(""))


def test_parse_result_rejects_non_json():
    with pytest.raises(claude_cli.ClaudeError):
        claude_cli.parse_result("not json at all")


def test_ask_runs_the_cli_and_returns_the_reply(tmp_path):
    script, log = fake_claude(tmp_path, result_json("the answer"))
    reply, session_id = claude_cli.ask(
        "the prompt", command=str(script), cwd=tmp_path / "wd")
    assert reply == "the answer"
    assert session_id == "sess-1"
    argv = log.read_text().splitlines()
    assert argv == ["-p", "--output-format", "json", "the prompt"]
    assert (tmp_path / "wd").is_dir()


def test_ask_resumes_with_the_previous_session(tmp_path):
    script, log = fake_claude(tmp_path, result_json("more", "sess-2"))
    reply, session_id = claude_cli.ask(
        "again", session_id="sess-1", model="sonnet", command=str(script))
    assert reply == "more"
    assert session_id == "sess-2"
    argv = log.read_text().splitlines()
    assert argv == ["-p", "--output-format", "json",
                    "--resume", "sess-1", "--model", "sonnet", "again"]


def test_ask_appends_the_system_prompt(tmp_path):
    script, log = fake_claude(tmp_path, result_json("ok"))
    claude_cli.ask("hi", command=str(script), system_prompt="討論に参加する")
    argv = log.read_text().splitlines()
    assert argv == ["-p", "--output-format", "json",
                    "--append-system-prompt", "討論に参加する", "hi"]


def test_ask_keeps_the_session_when_the_cli_omits_it(tmp_path):
    script, _ = fake_claude(tmp_path, result_json("more", session_id=None))
    _, session_id = claude_cli.ask(
        "again", session_id="sess-1", command=str(script))
    assert session_id == "sess-1"


def test_ask_reports_a_missing_binary():
    with pytest.raises(claude_cli.ClaudeError) as error:
        claude_cli.ask("hi", command="/nonexistent/claude")
    assert "not found" in str(error.value)


def test_ask_reports_a_failing_cli(tmp_path):
    script, _ = fake_claude(tmp_path, "boom", exit_code=3)
    with pytest.raises(claude_cli.ClaudeError) as error:
        claude_cli.ask("hi", command=str(script))
    assert "exited with 3" in str(error.value)
