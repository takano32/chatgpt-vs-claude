import json
import stat

from chatgpt_vs_claude.transcript import Transcript, render_markdown


def test_transcript_writes_json_and_markdown(tmp_path):
    transcript = Transcript(tmp_path / "out", "AIと創造性",
                            settings={"turns": 2})
    transcript.set_link("chatgpt", "https://chatgpt.com/c/abc")
    transcript.add("ChatGPT", 1, 2, "最初の主張")
    transcript.add("Claude", 1, 2, "反論")

    data = json.loads(transcript.json_path.read_text(encoding="utf-8"))
    assert data["topic"] == "AIと創造性"
    assert data["links"]["chatgpt"] == "https://chatgpt.com/c/abc"
    assert [turn["speaker"] for turn in data["turns"]] == ["ChatGPT", "Claude"]

    markdown = transcript.md_path.read_text(encoding="utf-8")
    assert "# ChatGPT vs Claude — AIと創造性" in markdown
    assert "## ChatGPT (1/2)" in markdown
    assert "最初の主張" in markdown
    assert "- turns: 2" in markdown


def test_transcript_files_are_private(tmp_path):
    transcript = Transcript(tmp_path / "out", "topic")
    directory_mode = stat.S_IMODE((tmp_path / "out").stat().st_mode)
    file_mode = stat.S_IMODE(transcript.json_path.stat().st_mode)
    assert directory_mode == 0o700
    assert file_mode == 0o600


def test_transcripts_in_the_same_second_get_distinct_paths(tmp_path):
    first = Transcript(tmp_path, "one")
    second = Transcript(tmp_path, "two")
    assert first.json_path != second.json_path
    assert first.md_path != second.md_path


def test_render_markdown_keeps_statement_order():
    markdown = render_markdown({
        "topic": "t",
        "started_at": "2026-08-20T00:00:00Z",
        "settings": {},
        "links": {},
        "turns": [
            {"speaker": "Claude", "index": 1, "total": 1, "text": "先手"},
            {"speaker": "ChatGPT", "index": 1, "total": 1, "text": "後手"},
        ],
    })
    assert markdown.index("先手") < markdown.index("後手")
