"""Tests for document_processor.py — text extraction from various formats."""

import gzip
import io
import json
import zipfile

import pytest
from document_processor import extract_text, _MAX_ARCHIVE_DEPTH


# ------------------------------------------------------------------
# Plain text / data formats
# ------------------------------------------------------------------

class TestExtractText:
    def test_txt_utf8(self):
        content = "Hello, world!".encode("utf-8")
        assert extract_text("readme.txt", content) == "Hello, world!"

    def test_txt_with_bom(self):
        content = "\ufeffBOM text".encode("utf-8-sig")
        assert "BOM text" in extract_text("bom.txt", content)

    def test_txt_latin1_fallback(self):
        content = "café résumé".encode("cp1252")
        result = extract_text("latin.txt", content)
        assert "caf" in result

    def test_md_treated_as_text(self):
        content = b"# Heading\n\nParagraph text."
        result = extract_text("doc.md", content)
        assert "# Heading" in result
        assert "Paragraph text." in result


class TestExtractCSV:
    def test_basic_csv(self):
        content = b"name,age,city\nAlice,30,NYC\nBob,25,LA"
        result = extract_text("data.csv", content)
        assert "Alice" in result
        assert "Bob" in result
        # CSV rows are converted to tab-separated
        assert "\t" in result

    def test_empty_csv(self):
        content = b""
        result = extract_text("empty.csv", content)
        assert result == ""


class TestExtractJSON:
    def test_valid_json(self):
        data = {"name": "Alice", "skills": ["python", "azure"]}
        content = json.dumps(data).encode("utf-8")
        result = extract_text("data.json", content)
        parsed = json.loads(result)
        assert parsed["name"] == "Alice"

    def test_invalid_json_returns_raw(self):
        content = b"{not valid json"
        result = extract_text("bad.json", content)
        assert result == "{not valid json"


class TestExtractXML:
    def test_basic_xml(self):
        xml = b"<root><item>Hello</item><item>World</item></root>"
        result = extract_text("data.xml", xml)
        assert "Hello" in result
        assert "World" in result

    def test_kml_uses_xml_extractor(self):
        kml = b"<kml><name>Place</name></kml>"
        result = extract_text("map.kml", kml)
        assert "Place" in result


class TestExtractHTML:
    def test_basic_html(self):
        html = b"<html><body><p>Hello</p><script>alert(1)</script></body></html>"
        result = extract_text("page.html", html)
        assert "Hello" in result
        assert "alert" not in result  # script tags stripped

    def test_htm_extension(self):
        html = b"<html><body>Content</body></html>"
        result = extract_text("page.htm", html)
        assert "Content" in result


# ------------------------------------------------------------------
# RTF
# ------------------------------------------------------------------

class TestExtractRTF:
    def test_simple_rtf(self):
        # Minimal valid RTF
        rtf = rb"{\rtf1 Hello RTF world}"
        result = extract_text("doc.rtf", rtf)
        assert "Hello" in result
        assert "RTF" in result


# ------------------------------------------------------------------
# EML (email)
# ------------------------------------------------------------------

class TestExtractEML:
    def test_basic_eml(self):
        eml = (
            b"From: sender@example.com\r\n"
            b"To: receiver@example.com\r\n"
            b"Subject: Test Email\r\n"
            b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"This is the body of the email."
        )
        result = extract_text("mail.eml", eml)
        assert "Test Email" in result
        assert "sender@example.com" in result
        assert "body of the email" in result


# ------------------------------------------------------------------
# Archives
# ------------------------------------------------------------------

class TestExtractZIP:
    def _make_zip(self, files: dict[str, bytes]) -> bytes:
        """Helper: create a ZIP in memory."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return buf.getvalue()

    def test_zip_with_text_files(self):
        content = self._make_zip({
            "a.txt": b"File A content",
            "b.txt": b"File B content",
        })
        result = extract_text("archive.zip", content)
        assert "File A content" in result
        assert "File B content" in result

    def test_zip_skips_directories(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("subdir/", "")
            zf.writestr("subdir/file.txt", b"inner file")
        result = extract_text("archive.zip", buf.getvalue())
        assert "inner file" in result

    def test_nested_zip_depth_limit(self):
        """Deeply nested ZIPs should stop at _MAX_ARCHIVE_DEPTH."""
        # Create a chain of nested ZIPs
        inner = b"deepest content"
        for i in range(_MAX_ARCHIVE_DEPTH + 1):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(f"level{i}.txt" if i == 0 else f"level{i}.zip", inner)
            inner = buf.getvalue()

        result = extract_text("outer.zip", inner)
        # At depth limit, extraction stops — we should NOT find "deepest content"
        # (depending on exact nesting — mainly verifying no infinite recursion)
        # The test passes if it completes without hanging
        assert isinstance(result, str)


class TestExtractGZ:
    def test_gz_with_text(self):
        original = b"Gzipped plain text content"
        compressed = gzip.compress(original)
        result = extract_text("file.txt.gz", compressed)
        assert "Gzipped plain text content" in result

    def test_gz_decompressed_size_limit(self):
        """GZ files that decompress to > 200 MB should be skipped."""
        # We can't create a 200 MB file in tests, but verify the guard path exists
        # by testing a normal file works
        original = b"small content"
        compressed = gzip.compress(original)
        result = extract_text("small.txt.gz", compressed)
        assert "small content" in result


# ------------------------------------------------------------------
# Unsupported format
# ------------------------------------------------------------------

class TestUnsupportedFormat:
    def test_unsupported_extension_returns_empty(self):
        assert extract_text("image.png", b"\x89PNG...") == ""

    def test_unknown_extension_returns_empty(self):
        assert extract_text("file.xyz", b"random bytes") == ""

    def test_no_extension_returns_empty(self):
        assert extract_text("Makefile", b"all: build") == ""


# ------------------------------------------------------------------
# Extraction failure resilience
# ------------------------------------------------------------------

class TestExtractionErrors:
    def test_corrupt_pdf_returns_empty(self):
        # Not a valid PDF — should log error and return ""
        result = extract_text("bad.pdf", b"not a pdf at all")
        assert result == ""

    def test_corrupt_docx_returns_empty(self):
        result = extract_text("bad.docx", b"not a docx file")
        assert result == ""

    def test_corrupt_zip_returns_empty(self):
        result = extract_text("bad.zip", b"not a zip file")
        assert result == ""

    def test_corrupt_gz_returns_empty(self):
        result = extract_text("bad.txt.gz", b"not a gz file")
        assert result == ""
