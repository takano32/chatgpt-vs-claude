import pytest

from chatgpt_vs_claude import debate


class FakeSpeaker:
    def __init__(self, name):
        self.name = name
        self.prompts = []

    def say(self, prompt):
        self.prompts.append(prompt)
        return f"{self.name} statement {len(self.prompts)}"


def test_prompts_carry_topic_rules_and_opponent():
    prompt = debate.opening_prompt("AIと仕事", "Claude", 3, 400,
                                   instructions="英語で議論する")
    assert "AIと仕事" in prompt
    assert "Claude" in prompt
    assert "合計 3 回" in prompt
    assert "400 字以内" in prompt
    assert "英語で議論する" in prompt
    assert debate.FINAL_NOTE not in prompt


def test_relay_prompt_marks_the_last_statement():
    normal = debate.relay_prompt("ChatGPT", "相手の主張")
    final = debate.relay_prompt("ChatGPT", "相手の主張", final=True)
    assert "相手の主張" in normal
    assert debate.FINAL_NOTE not in normal
    assert debate.FINAL_NOTE in final


def test_run_debate_alternates_and_relays_verbatim():
    first = FakeSpeaker("ChatGPT")
    second = FakeSpeaker("Claude")
    statements = debate.run_debate(
        first=first, second=second, topic="テーマ", turns=2)

    assert [s["speaker"] for s in statements] == [
        "ChatGPT", "Claude", "ChatGPT", "Claude"]
    # The opener gets the opening prompt; the responder's first prompt
    # introduces the debate and quotes the opener verbatim.
    assert "まず、このテーマに対する" in first.prompts[0]
    assert "ChatGPT statement 1" in second.prompts[0]
    assert "テーマ" in second.prompts[0]
    # Later prompts are plain relays of the newest statement.
    assert "Claude statement 1" in first.prompts[1]
    assert "まず" not in first.prompts[1]


def test_run_debate_marks_each_sides_last_statement():
    first = FakeSpeaker("ChatGPT")
    second = FakeSpeaker("Claude")
    debate.run_debate(first=first, second=second, topic="t", turns=3)
    assert debate.FINAL_NOTE not in first.prompts[0]
    assert debate.FINAL_NOTE not in first.prompts[1]
    assert debate.FINAL_NOTE in first.prompts[2]
    assert debate.FINAL_NOTE not in second.prompts[1]
    assert debate.FINAL_NOTE in second.prompts[2]


def test_run_debate_records_and_reports_every_statement():
    events = []

    class RecordingTranscript:
        def add(self, speaker, index, total, text):
            events.append(("transcript", speaker, index, total, text))

    debate.run_debate(
        first=FakeSpeaker("A"), second=FakeSpeaker("B"),
        topic="t", turns=2,
        transcript=RecordingTranscript(),
        on_statement=lambda *args: events.append(("shown",) + args),
    )
    transcript_events = [e for e in events if e[0] == "transcript"]
    shown_events = [e for e in events if e[0] == "shown"]
    assert len(transcript_events) == 4
    assert len(shown_events) == 4
    assert transcript_events[0][1:4] == ("A", 1, 2)
    assert transcript_events[-1][1:4] == ("B", 2, 2)


def test_run_debate_rejects_zero_turns():
    with pytest.raises(ValueError):
        debate.run_debate(first=FakeSpeaker("A"), second=FakeSpeaker("B"),
                          topic="t", turns=0)
