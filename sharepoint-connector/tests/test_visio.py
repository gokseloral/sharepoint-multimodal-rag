"""Tests for the Visio (.vsdx) extraction path."""

import io
import zipfile

from document_processor import extract_text
from visio_processor import vsdx_to_text, extract_visio_text


_NS = "http://schemas.microsoft.com/office/visio/2012/main"


def _page_xml(*labels: str) -> bytes:
    shapes = "".join(
        f'<Shape><Text>{label}</Text></Shape>' for label in labels
    )
    return (
        f'<PageContents xmlns="{_NS}"><Shapes>{shapes}</Shapes></PageContents>'
    ).encode("utf-8")


def _make_vsdx(pages: list[list[str]], masters: list[str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for idx, labels in enumerate(pages, start=1):
            zf.writestr(f"visio/pages/page{idx}.xml", _page_xml(*labels))
        if masters:
            zf.writestr("visio/masters/master1.xml", _page_xml(*masters))
    return buf.getvalue()


class TestVsdxToText:
    def test_single_page_labels(self, tmp_path):
        vsdx = tmp_path / "diagram.vsdx"
        vsdx.write_bytes(_make_vsdx([["Start", "Process", "End"]]))

        text = vsdx_to_text(str(vsdx))

        assert "--- Page 1 ---" in text
        assert "Start" in text
        assert "Process" in text
        assert "End" in text

    def test_multiple_pages(self, tmp_path):
        vsdx = tmp_path / "multi.vsdx"
        vsdx.write_bytes(_make_vsdx([["Page one shape"], ["Page two shape"]]))

        text = vsdx_to_text(str(vsdx))

        assert "--- Page 1 ---" in text
        assert "--- Page 2 ---" in text
        assert "Page one shape" in text
        assert "Page two shape" in text

    def test_master_stencil_labels(self, tmp_path):
        vsdx = tmp_path / "stencil.vsdx"
        vsdx.write_bytes(_make_vsdx([["Canvas label"]], masters=["Stencil label"]))

        text = vsdx_to_text(str(vsdx))

        assert "--- Stencils ---" in text
        assert "Stencil label" in text

    def test_dedupes_repeated_labels(self, tmp_path):
        vsdx = tmp_path / "dupes.vsdx"
        vsdx.write_bytes(_make_vsdx([["Box", "Box", "Box"]]))

        text = vsdx_to_text(str(vsdx))

        assert text.count("Box") == 1

    def test_invalid_zip_returns_empty(self, tmp_path):
        bad = tmp_path / "broken.vsdx"
        bad.write_bytes(b"not a zip file")

        assert vsdx_to_text(str(bad)) == ""


class TestExtractVisioDispatch:
    def test_unsupported_extension(self, tmp_path):
        f = tmp_path / "diagram.txt"
        f.write_bytes(b"")
        assert extract_visio_text(str(f), "diagram.txt") == ""


class TestExtractTextRoutesVisio:
    def test_extract_text_path_routes_vsdx(self, tmp_path):
        vsdx = tmp_path / "flow.vsdx"
        vsdx.write_bytes(_make_vsdx([["Routed via extract_text"]]))

        text = extract_text("flow.vsdx", None, path=str(vsdx))

        assert "Routed via extract_text" in text

    def test_extract_text_bytes_routes_vsdx(self):
        content = _make_vsdx([["Routed via bytes"]])

        text = extract_text("flow.vsdx", content)

        assert "Routed via bytes" in text
