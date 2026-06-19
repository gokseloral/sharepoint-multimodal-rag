"""Tests for the video transcription path — Speech client helpers and routing."""

from blocks import Block, BlockKind
from speech_transcription_client import _fmt_ts, _iso_to_seconds, _phrases_to_blocks
from document_processor import extract_blocks


# ------------------------------------------------------------------
# Timestamp helpers
# ------------------------------------------------------------------

class TestFmtTs:
    def test_under_one_hour(self):
        assert _fmt_ts(0) == "00:00"
        assert _fmt_ts(90) == "01:30"
        assert _fmt_ts(3599) == "59:59"

    def test_over_one_hour(self):
        assert _fmt_ts(3661) == "01:01:01"


class TestIsoToSeconds:
    def test_seconds_only(self):
        assert _iso_to_seconds("PT30S") == 30.0

    def test_minutes_and_seconds(self):
        assert _iso_to_seconds("PT1M30.5S") == 90.5

    def test_hours_minutes_seconds(self):
        assert _iso_to_seconds("PT1H2M3S") == 3723.0

    def test_empty_returns_zero(self):
        assert _iso_to_seconds("") == 0.0
        assert _iso_to_seconds(None) == 0.0  # type: ignore[arg-type]


# ------------------------------------------------------------------
# Phrase → Block grouping
# ------------------------------------------------------------------

class TestPhrasesToBlocks:
    def test_single_window(self):
        phrases = [
            {"offset": "PT0S", "text": "Hello."},
            {"offset": "PT5S", "text": "World."},
        ]
        blocks = _phrases_to_blocks(phrases, segment_seconds=60)
        assert len(blocks) == 1
        assert "Hello." in blocks[0].text
        assert "World." in blocks[0].text
        assert blocks[0].kind == BlockKind.TEXT

    def test_splits_across_windows(self):
        phrases = [
            {"offset": "PT0S", "text": "First."},
            {"offset": "PT61S", "text": "Second."},
        ]
        blocks = _phrases_to_blocks(phrases, segment_seconds=60)
        assert len(blocks) == 2
        assert "First." in blocks[0].text
        assert "Second." in blocks[1].text

    def test_timestamp_prefix(self):
        phrases = [{"offset": "PT0S", "text": "Hi."}]
        blocks = _phrases_to_blocks(phrases, segment_seconds=60)
        assert "[00:00]" in blocks[0].text

    def test_empty_phrases(self):
        assert _phrases_to_blocks([], segment_seconds=60) == []

    def test_skips_blank_text(self):
        phrases = [
            {"offset": "PT0S", "text": ""},
            {"offset": "PT1S", "text": "Real text."},
        ]
        blocks = _phrases_to_blocks(phrases, segment_seconds=60)
        assert len(blocks) == 1
        assert "Real text." in blocks[0].text


# ------------------------------------------------------------------
# Routing in extract_blocks
# ------------------------------------------------------------------

class _FakeTranscriber:
    enabled = True

    def __init__(self):
        self.called_with = None

    def extract_blocks(self, path, ext):
        self.called_with = (path, ext)
        return [Block(kind=BlockKind.TEXT, order=0, text="transcript text")]


class TestVideoRouting:
    def test_video_routes_to_transcriber(self, tmp_path):
        video = tmp_path / "demo.mp4"
        video.write_bytes(b"\x00\x00")
        fake = _FakeTranscriber()

        blocks = extract_blocks(str(video), "demo.mp4", video_transcriber=fake)

        assert fake.called_with == (str(video), ".mp4")
        assert len(blocks) == 1
        assert blocks[0].kind == BlockKind.TEXT
        assert blocks[0].text == "transcript text"

    def test_video_skipped_when_transcriber_not_supplied(self, tmp_path):
        video = tmp_path / "demo.mp4"
        video.write_bytes(b"\x00\x00")

        assert extract_blocks(str(video), "demo.mp4") == []

    def test_video_skipped_when_transcriber_disabled(self, tmp_path):
        video = tmp_path / "demo.mp4"
        video.write_bytes(b"\x00\x00")

        class _Disabled:
            enabled = False
            def extract_blocks(self, path, ext): ...  # pragma: no cover

        assert extract_blocks(str(video), "demo.mp4", video_transcriber=_Disabled()) == []

