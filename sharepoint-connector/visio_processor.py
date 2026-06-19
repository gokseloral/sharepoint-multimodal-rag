"""
Visio (.vsdx / .vsd) → plain-text extraction.

.vsdx (modern, Open Packaging Conventions ZIP):
    Pure standard-library extraction. A .vsdx is a ZIP whose drawing pages live
    under `visio/pages/page*.xml`. Shape text is held in `<Text>` elements
    (optionally interleaved with `<cp>` / `<pp>` / `<fld>` formatting markers,
    which we strip). Master shapes under `visio/masters/master*.xml` are also
    scanned so stencil-provided labels are captured. No third-party package is
    required, so this works on Azure Functions Flex Consumption out of the box.

.vsd (legacy binary OLE compound document):
    There is no reliable pure-Python reader. If the LibreOffice `soffice` binary
    is available on PATH we convert .vsd → .vsdx in a temp dir and parse that.
    When `soffice` is absent (the default on Azure Functions) the file is
    skipped with a clear warning rather than failing the whole indexer run.

Returned text is a newline-joined, de-duplicated, reading-order-ish list of the
shape labels on each page, prefixed with a `--- Page N ---` separator. This is
indexed as ordinary TEXT (a single TEXT block → text chunks), so Visio diagrams
become searchable by their on-canvas labels.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

logger = logging.getLogger(__name__)

# Visio 2013+ drawing XML namespace.
_NS = "{http://schemas.microsoft.com/office/visio/2012/main}"

# Inline formatting markers that may appear between text runs inside <Text>.
_INLINE_MARKERS = ("cp", "pp", "tp", "fld")

VSDX_EXT = ".vsdx"
VSD_EXT = ".vsd"


def _shape_text(text_el: ET.Element) -> str:
    """Concatenate the visible runs of a Visio <Text> element, dropping the
    inline `<cp>`/`<pp>`/`<tp>`/`<fld>` formatting markers."""
    parts: list[str] = []
    if text_el.text:
        parts.append(text_el.text)
    for child in text_el:
        tag = child.tag.split("}")[-1]
        if tag in _INLINE_MARKERS:
            # Field placeholders carry no literal text; keep any tail content.
            if child.tail:
                parts.append(child.tail)
        else:
            if child.text:
                parts.append(child.text)
            if child.tail:
                parts.append(child.tail)
    return "".join(parts).strip()


def _texts_from_page_xml(raw: bytes) -> list[str]:
    """Return the ordered shape labels found in one page/master XML part."""
    labels: list[str] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.warning(f"Visio: could not parse page XML: {e}")
        return labels

    # Namespaced lookup first; fall back to a namespace-agnostic scan so we
    # tolerate older/newer schema revisions.
    text_els = root.iter(f"{_NS}Text")
    found_any = False
    for text_el in text_els:
        found_any = True
        label = _shape_text(text_el)
        if label:
            labels.append(label)
    if not found_any:
        for el in root.iter():
            if el.tag.split("}")[-1] == "Text":
                label = _shape_text(el)
                if label:
                    labels.append(label)
    return labels


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def vsdx_to_text(path: str) -> str:
    """Extract text from a .vsdx file using only the standard library."""
    page_outputs: list[str] = []
    master_labels: list[str] = []

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()

            master_parts = sorted(
                n for n in names
                if re.match(r"visio/masters/master\d+\.xml$", n, re.IGNORECASE)
            )
            for part in master_parts:
                master_labels.extend(_texts_from_page_xml(zf.read(part)))

            page_parts = sorted(
                (n for n in names if re.match(r"visio/pages/page\d+\.xml$", n, re.IGNORECASE)),
                key=lambda n: int(re.search(r"page(\d+)\.xml$", n, re.IGNORECASE).group(1)),
            )
            for idx, part in enumerate(page_parts, start=1):
                labels = _dedupe_preserve_order(_texts_from_page_xml(zf.read(part)))
                if labels:
                    page_outputs.append(f"--- Page {idx} ---\n" + "\n".join(labels))
    except zipfile.BadZipFile as e:
        logger.error(f"Visio: {path} is not a valid .vsdx (ZIP) file: {e}")
        return ""
    except Exception as e:  # noqa: BLE001
        logger.error(f"Visio: failed to extract {path}: {e}")
        return ""

    sections: list[str] = []
    extra_masters = _dedupe_preserve_order(master_labels)
    if extra_masters:
        sections.append("--- Stencils ---\n" + "\n".join(extra_masters))
    sections.extend(page_outputs)
    return "\n\n".join(sections)


def _find_soffice() -> str | None:
    """Locate the LibreOffice headless binary, if installed."""
    for candidate in ("soffice", "libreoffice"):
        found = shutil.which(candidate)
        if found:
            return found
    env_path = os.getenv("SOFFICE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    return None


def vsd_to_text(path: str) -> str:
    """Extract text from a legacy binary .vsd by converting it to .vsdx via
    LibreOffice first. Returns "" (with a warning) when soffice is unavailable."""
    soffice = _find_soffice()
    if not soffice:
        logger.warning(
            "Visio: %s is a legacy binary .vsd and LibreOffice (soffice) is not "
            "available; skipping. Install LibreOffice or set SOFFICE_PATH, or "
            "re-save the file as .vsdx to index it.",
            path,
        )
        return ""

    tmpdir = tempfile.mkdtemp(prefix="vsd-")
    try:
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "vsdx", "--outdir", tmpdir, path],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            logger.error(f"Visio: soffice conversion failed for {path}: {result.stderr[:500]}")
            return ""
        stem = os.path.splitext(os.path.basename(path))[0]
        converted = os.path.join(tmpdir, f"{stem}.vsdx")
        if not os.path.exists(converted):
            logger.error(f"Visio: soffice produced no .vsdx for {path}")
            return ""
        return vsdx_to_text(converted)
    except subprocess.TimeoutExpired:
        logger.error(f"Visio: soffice conversion timed out for {path}")
        return ""
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def extract_visio_text(path: str, filename: str = "") -> str:
    """Dispatch to the .vsdx or .vsd extractor based on extension."""
    ext = os.path.splitext(filename or path)[1].lower()
    if ext == VSDX_EXT:
        return vsdx_to_text(path)
    if ext == VSD_EXT:
        return vsd_to_text(path)
    logger.warning(f"Visio: unsupported extension for {filename or path}")
    return ""
