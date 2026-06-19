"""Tests for the merged video path — Content Understanding routing + rendering."""

from blocks import BlockKind
from content_understanding_client import _format_timestamp, _segment_to_text
from document_processor import extract_blocks


# ------------------------------------------------------------------
# Segment rendering
# ------------------------------------------------------------------

class TestFormatTimestamp:
    def test_under_one_hour(self):
        assert _format_timestamp(0) == "00:00"
        assert _format_timestamp(90_000) == "01:30"

    def test_over_one_hour(self):
        assert _format_timestamp(3_661_000) == "01:01:01"


class TestSegmentToText:
    def test_summary_and_transcript(self):
        segment = {
            "startTimeMs": 0,
            "endTimeMs": 12_000,
            "fields": {"Summary": {"valueString": "Intro to Copilot Studio."}},
            "transcriptPhrases": [
                {"speaker": "Speaker 1", "text": "Welcome to the demo."},
                {"speaker": "Speaker 1", "text": "Let's get started."},
            ],
        }
        text = _segment_to_text(segment)
        assert "[00:00–00:12]" in text
        assert "Summary: Intro to Copilot Studio." in text
        assert "Speaker 1: Welcome to the demo." in text
        assert "Let's get started." in text

    def test_falls_back_to_markdown(self):
        segment = {"markdown": "Some visual-only content."}
        assert _segment_to_text(segment) == "Some visual-only content."

    def test_empty_segment(self):
        assert _segment_to_text({}) == ""


# ------------------------------------------------------------------
# Routing in extract_blocks
# ------------------------------------------------------------------

class _FakeCU:
    enabled = True

    def __init__(self):
        self.called_with = None

    def extract_blocks(self, path, ext):
        from blocks import Block
        self.called_with = (path, ext)
        return [Block(kind=BlockKind.TEXT, order=0, text="transcript text")]


class TestVideoRouting:
    def test_video_routes_to_content_understanding(self, tmp_path):
        video = tmp_path / "demo.mp4"
        video.write_bytes(b"\x00\x00")
        fake = _FakeCU()

        blocks = extract_blocks(str(video), "demo.mp4", content_understanding=fake)

        assert fake.called_with == (str(video), ".mp4")
        assert len(blocks) == 1
        assert blocks[0].kind == BlockKind.TEXT
        assert blocks[0].text == "transcript text"

    def test_video_skipped_when_cu_disabled(self, tmp_path):
        video = tmp_path / "demo.mp4"
        video.write_bytes(b"\x00\x00")

        # No content_understanding client supplied → video is skipped.
        assert extract_blocks(str(video), "demo.mp4") == []
