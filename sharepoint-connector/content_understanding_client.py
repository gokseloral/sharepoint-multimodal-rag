"""
Azure AI Content Understanding — video analyzer (prebuilt-videoSearch).

Turns a video file into ordered TEXT blocks (transcript + per-segment summary)
so the existing multimodal pipeline can chunk, embed, and index it exactly like
any document: videos in SharePoint flow through Content Understanding
instead of Document Intelligence, then re-join the unified index.

REST API (Foundry / Azure AI Services endpoint):
  POST {endpoint}/contentunderstanding/analyzers/{analyzerId}:analyzeBinary
        ?api-version=2025-11-01
    body: raw video bytes
    -> 202 Accepted with an `Operation-Location` header
  GET  {Operation-Location}            (poll until status == succeeded/failed)

The succeeded result exposes `result.contents[]`, where each content item is a
video segment with:
  - fields.Summary.valueString : one-paragraph generative summary
  - transcriptPhrases[]        : { speaker, startTimeMs, endTimeMs, text }
  - startTimeMs / endTimeMs    : segment span
  - markdown                   : RAG-ready markdown (transcript + key frames)

Auth: DefaultAzureCredential → bearer token for scope
https://cognitiveservices.azure.com/.default (Cognitive Services User RBAC role).
"""

from __future__ import annotations

import logging
import time

import httpx
from azure.identity import DefaultAzureCredential

from blocks import Block, BlockKind
from config import ContentUnderstandingConfig

logger = logging.getLogger(__name__)

_SCOPE = "https://cognitiveservices.azure.com/.default"

# Video container formats routed to Content Understanding.
VIDEO_SUPPORTED_EXTS: frozenset[str] = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".webm",
})

_CONTENT_TYPE_BY_EXT: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".wmv": "video/x-ms-wmv",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
}


class ContentUnderstandingClient:
    """Thin wrapper around the Content Understanding video analyzer that returns
    blocks.Block objects ready for the chunker."""

    def __init__(
        self,
        config: ContentUnderstandingConfig,
        credential: DefaultAzureCredential | None = None,
    ):
        self._cfg = config
        self._credential = credential or DefaultAzureCredential()
        self._endpoint = (config.endpoint or "").rstrip("/")
        self._http = httpx.Client(timeout=60.0)
        self._token: str | None = None
        self._token_expires_on: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.endpoint)

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _bearer(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_on - 60:
            return self._token
        token = self._credential.get_token(_SCOPE)
        self._token = token.token
        self._token_expires_on = float(token.expires_on)
        return self._token

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def extract_blocks(self, file_path: str, ext: str) -> list[Block]:
        """Analyze a video file and convert each segment into a TEXT Block.

        Returns an empty list if Content Understanding is not configured, the
        extension is unsupported, or the service fails.
        """
        if not self.enabled:
            return []
        if ext.lower() not in VIDEO_SUPPORTED_EXTS:
            return []

        try:
            result = self._analyze(file_path, ext)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Content Understanding failed for {file_path}: {e}")
            return []

        contents = (
            (result or {}).get("result", {}).get("contents", []) or []
        )
        if not contents:
            logger.warning(f"Content Understanding returned no contents for {file_path}")
            return []

        blocks: list[Block] = []
        order = 0
        for segment in contents:
            text = _segment_to_text(segment)
            if not text:
                continue
            blocks.append(Block(kind=BlockKind.TEXT, order=order, text=text))
            order += 1

        logger.info(
            f"Content Understanding extracted {len(blocks)} segment block(s) from {file_path}"
        )
        return blocks

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _analyze(self, file_path: str, ext: str) -> dict:
        """POST the video bytes, then poll Operation-Location until terminal."""
        analyze_url = (
            f"{self._endpoint}/contentunderstanding/analyzers/"
            f"{self._cfg.analyzer_id}:analyzeBinary?api-version={self._cfg.api_version}"
        )
        content_type = _CONTENT_TYPE_BY_EXT.get(ext.lower(), "application/octet-stream")

        with open(file_path, "rb") as f:
            resp = self._http.post(
                analyze_url,
                content=f.read(),
                headers={
                    "Authorization": f"Bearer {self._bearer()}",
                    "Content-Type": content_type,
                },
            )
        resp.raise_for_status()

        operation_location = resp.headers.get("Operation-Location")
        if not operation_location:
            raise RuntimeError("Content Understanding did not return an Operation-Location header")

        return self._poll(operation_location)

    def _poll(self, operation_location: str) -> dict:
        deadline = time.time() + self._cfg.poll_timeout_seconds
        while True:
            resp = self._http.get(
                operation_location,
                headers={"Authorization": f"Bearer {self._bearer()}"},
            )
            resp.raise_for_status()
            body = resp.json()
            status = str(body.get("status", "")).lower()

            if status == "succeeded":
                return body
            if status == "failed":
                raise RuntimeError(f"Content Understanding analysis failed: {body}")

            if time.time() >= deadline:
                raise TimeoutError(
                    f"Content Understanding analysis timed out after "
                    f"{self._cfg.poll_timeout_seconds}s (last status={status!r})"
                )
            time.sleep(self._cfg.poll_interval_seconds)

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Result -> text
# ---------------------------------------------------------------------------


def _format_timestamp(ms: int) -> str:
    total_seconds = max(0, int(ms) // 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _segment_to_text(segment: dict) -> str:
    """Render one Content Understanding video segment as grounding text.

    Combines the segment time span, the generative summary, and the speaker-
    attributed transcript so a single chunk carries everything needed for a
    cited, time-aware answer.
    """
    parts: list[str] = []

    start = segment.get("startTimeMs")
    end = segment.get("endTimeMs")
    if start is not None and end is not None:
        parts.append(f"## Video segment [{_format_timestamp(start)}–{_format_timestamp(end)}]")

    fields = segment.get("fields") or {}
    summary = (fields.get("Summary") or {}).get("valueString", "").strip()
    if summary:
        parts.append(f"Summary: {summary}")

    phrases = segment.get("transcriptPhrases") or []
    transcript_lines: list[str] = []
    for phrase in phrases:
        phrase_text = (phrase.get("text") or "").strip()
        if not phrase_text:
            continue
        speaker = (phrase.get("speaker") or "").strip()
        transcript_lines.append(f"{speaker}: {phrase_text}" if speaker else phrase_text)
    if transcript_lines:
        parts.append("Transcript:\n" + "\n".join(transcript_lines))

    # Fall back to the analyzer markdown if there was neither summary nor
    # transcript (e.g. a silent clip with only visual key frames).
    if not summary and not transcript_lines:
        markdown = (segment.get("markdown") or "").strip()
        if markdown:
            parts.append(markdown)

    return "\n\n".join(parts).strip()
