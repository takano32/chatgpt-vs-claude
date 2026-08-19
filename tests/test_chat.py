import pytest

from chatgpt_vs_claude import chat

from conftest import FakeSession, ok


def message(role, text, *, status="finished_successfully",
            content_type="text", create_time=1.0, recipient=None,
            end_turn=None, metadata=None):
    payload = {
        "author": {"role": role},
        "content": {"content_type": content_type, "parts": [text]},
        "status": status,
        "create_time": create_time,
    }
    if recipient is not None:
        payload["recipient"] = recipient
    if end_turn is not None:
        payload["end_turn"] = end_turn
    if metadata is not None:
        payload["metadata"] = metadata
    return {"message": payload}


def test_conversation_id_from_url():
    assert chat.conversation_id_from_url(
        "https://chatgpt.com/c/6890a7c2-1234-8005-a5ea-1b2c3d4e5f60"
    ) == "6890a7c2-1234-8005-a5ea-1b2c3d4e5f60"
    assert chat.conversation_id_from_url("https://chatgpt.com/") is None


def test_latest_assistant_text_picks_the_newest_finished_text():
    conversation = {
        "mapping": {
            "u": message("user", "the prompt", create_time=1.0),
            "a1": message("assistant", "first draft", create_time=2.0),
            "a2": message("assistant", "final answer", create_time=3.0),
        }
    }
    text, finished, create_time = chat.latest_assistant_text(conversation)
    assert text == "final answer"
    assert finished
    assert create_time == 3.0


def test_latest_assistant_text_ignores_thoughts_and_tools():
    conversation = {
        "mapping": {
            "t": message("assistant", "thinking...",
                         content_type="thoughts", create_time=5.0),
            "tool": message("tool", "tool output", create_time=6.0),
            "side": message("assistant", "to a tool", recipient="bio",
                            create_time=7.0),
            "a": message("assistant", "the reply", create_time=2.0),
        }
    }
    text, finished, _ = chat.latest_assistant_text(conversation)
    assert text == "the reply"
    assert finished


def test_latest_assistant_text_respects_after_time():
    conversation = {
        "mapping": {
            "a1": message("assistant", "old reply", create_time=2.0),
        }
    }
    text, finished, _ = chat.latest_assistant_text(conversation,
                                                   after_time=2.0)
    assert text == ""
    assert not finished

    conversation["mapping"]["a2"] = message(
        "assistant", "new reply", create_time=3.0)
    text, finished, create_time = chat.latest_assistant_text(
        conversation, after_time=2.0)
    assert text == "new reply"
    assert finished
    assert create_time == 3.0


def test_latest_assistant_text_reports_streaming_as_unfinished():
    conversation = {
        "mapping": {
            "a": message("assistant", "typing", status="in_progress",
                         create_time=2.0),
        }
    }
    text, finished, _ = chat.latest_assistant_text(conversation)
    assert text == "typing"
    assert not finished


def test_latest_assistant_text_waits_on_continuation_chunks():
    conversation = {
        "mapping": {
            "a": message("assistant", "part one", end_turn=False,
                         create_time=2.0),
        }
    }
    _, finished, _ = chat.latest_assistant_text(conversation)
    assert not finished


def test_latest_assistant_text_joins_split_replies():
    conversation = {
        "mapping": {
            "a1": message("assistant", "part one", end_turn=False,
                          create_time=2.0),
            "a2": message("assistant", "part two", end_turn=True,
                          create_time=3.0),
        }
    }
    text, finished, create_time = chat.latest_assistant_text(conversation)
    assert text == "part one\n\npart two"
    assert finished
    assert create_time == 3.0


def test_latest_assistant_text_does_not_join_complete_turns():
    # Two turn-ending messages are separate turns (e.g. an old branch and
    # a regenerated reply), not chunks of one statement.
    conversation = {
        "mapping": {
            "a1": message("assistant", "old turn", create_time=2.0),
            "a2": message("assistant", "new turn", create_time=3.0),
        }
    }
    text, _, _ = chat.latest_assistant_text(conversation)
    assert text == "new turn"


def test_latest_assistant_text_accepts_token_capped_replies():
    conversation = {
        "mapping": {
            "a": message("assistant", "cut short", end_turn=False,
                         create_time=2.0,
                         metadata={"finish_details": {"type": "max_tokens"}}),
        }
    }
    _, finished, _ = chat.latest_assistant_text(conversation)
    assert finished


def test_wait_for_reply_skips_the_previous_reply():
    conversations = [
        ok({"mapping": {
            "a1": message("assistant", "old reply", create_time=2.0),
        }}),
        ok({"mapping": {
            "a1": message("assistant", "old reply", create_time=2.0),
            "a2": message("assistant", "new reply", create_time=5.0),
        }}),
    ]
    session = FakeSession(lambda method, path, body: conversations)
    text, create_time = chat.wait_for_reply(
        session, "conv-1", after_time=2.0,
        timeout_seconds=5, poll_seconds=0)
    assert text == "new reply"
    assert create_time == 5.0
    assert len(session.calls) == 2


def test_wait_for_reply_times_out_with_a_clear_error():
    session = FakeSession(lambda method, path, body: ok({"mapping": {}}))
    with pytest.raises(RuntimeError) as error:
        chat.wait_for_reply(session, "conv-1", timeout_seconds=0,
                            poll_seconds=0)
    assert "conv-1" in str(error.value)
