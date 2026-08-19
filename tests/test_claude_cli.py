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


def fake_stream_claude(tmp_path, die_after=None, error_turn=None):
    """A fake streaming CLI: echoes each stdin message as a result event."""
    log_path = tmp_path / "stream-argv.log"
    script_path = tmp_path / "claude-stream"
    script_path.write_text(f'''#!/usr/bin/env python3
import json, sys
with open({str(log_path)!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
n = 0
for line in sys.stdin:
    n += 1
    text = json.loads(line)["message"]["content"][0]["text"]
    print(json.dumps({{"type": "system", "subtype": "init"}}), flush=True)
    is_error = {error_turn!r} == n
    print(json.dumps({{"type": "result", "subtype": "success",
                       "is_error": is_error,
                       "result": "boom" if is_error else f"echo{{n}}:{{text}}",
                       "session_id": "sess-stream"}}), flush=True)
    if {die_after!r} == n:
        sys.exit(1)
''')
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    return script_path, log_path


def test_process_answers_multiple_turns_in_one_process(tmp_path):
    script, log = fake_stream_claude(tmp_path)
    process = claude_cli.ClaudeProcess(command=str(script),
                                       cwd=tmp_path / "wd")
    try:
        assert process.ask("一") == "echo1:一"
        assert process.ask("二") == "echo2:二"
        assert process.session_id == "sess-stream"
    finally:
        process.close()
    # The rising counter proves both turns hit the same process,
    # and only one spawn was logged.
    assert len(log.read_text().splitlines()) == 1


def test_process_respawns_with_resume_after_a_crash(tmp_path):
    script, log = fake_stream_claude(tmp_path, die_after=1)
    process = claude_cli.ClaudeProcess(command=str(script))
    try:
        assert process.ask("一") == "echo1:一"
        assert process.ask("二") == "echo1:二"
    finally:
        process.close()
    spawns = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(spawns) == 2
    assert "--resume" not in spawns[0]
    resume_index = spawns[1].index("--resume")
    assert spawns[1][resume_index + 1] == "sess-stream"


def test_process_passes_model_and_system_prompt(tmp_path):
    script, log = fake_stream_claude(tmp_path)
    process = claude_cli.ClaudeProcess(command=str(script), model="haiku",
                                       system_prompt="討論に参加する")
    try:
        process.ask("hi")
    finally:
        process.close()
    argv = json.loads(log.read_text().splitlines()[0])
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--append-system-prompt") + 1] == "討論に参加する"


def test_process_surfaces_model_errors_without_respawn(tmp_path):
    script, log = fake_stream_claude(tmp_path, error_turn=1)
    process = claude_cli.ClaudeProcess(command=str(script))
    try:
        with pytest.raises(claude_cli.ClaudeError) as error:
            process.ask("hi")
    finally:
        process.close()
    assert "boom" in str(error.value)
    assert len(log.read_text().splitlines()) == 1


def test_process_reports_a_missing_binary():
    process = claude_cli.ClaudeProcess(command="/nonexistent/claude")
    with pytest.raises(claude_cli.ClaudeError) as error:
        process.ask("hi")
    assert "not found" in str(error.value)
