"""Azure AI Speech — video/audio transcription via Fast Transcription REST API.

Replaces Azure AI Content Understanding (prebuilt-videoSearch), which is not
available in Canada Central.  Uses the same Azure AI Services (Foundry)
multi-service endpoint already provisioned for Azure OpenAI — no extra Azure
resource or RBAC assignment is required.

Pipeline
--------
1. Extract the first audio track from the video file using PyAV.  PyAV ships
   its own FFmpeg binaries inside the wheel, so no system package is needed.
2. Re-encode as 16 kHz, mono, 16-bit PCM WAV to keep the payload well within
   the Speech API's 200 MB per-request limit.
3. POST the WAV bytes to the Speech Fast Transcription endpoint — synchronous,
   single HTTP call, no polling, no blob-storage dependency.
4. Group the phrase-level timestamps into ~60-second TEXT blocks so the
   chunker/embed/index path handles video exactly like documents.

REST endpoint (reuses the existing Foundry / AIServices multi-service account)
  POST {endpoint}/speechtotext/transcriptions:transcribe
       ?api-version=2024-11-15
  Content-Type: multipart/form-data
    definition : JSON — {"locales": ["en-US"], "profanityFilterMode": "None"}
    audio      : WAV bytes
  → 200 OK   { "phrases": [{offset, duration, text}, …], … }

Auth: DefaultAzureCredential → bearer for
      https://cognitiveservices.azure.com/.default
      (Cognitive Services User role — already assigned by the Bicep template
      for Azure OpenAI operations on the same account).
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import uuid

import httpx
from azure.identity import DefaultAzureCredential

from blocks import Block, BlockKind
from config import SpeechTranscriptionConfig

logger = logging.getLogger(__name__)

_SCOPE = "https://cognitiveservices.azure.com/.default"

# Video container formats accepted by this client.
VIDEO_SUPPORTED_EXTS: frozenset[str] = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".webm",
})


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------

def _extract_audio_wav(video_path: str) -> bytes:
    """Return the first audio track of a video re-encoded as 16 kHz mono WAV.

    Uses PyAV which bundles its own FFmpeg libraries — no system dependency.

    Raises
    ------
    ImportError  – if the ``av`` package is not installed.
    RuntimeError – if the file contains no audio track.
    """
    try:
        import av  # noqa: PLC0415 — deferred to give a helpful ImportError
    except ImportError as exc:
        raise ImportError(
            "PyAV (av) is not installed. Add 'av>=13.0.0' to project dependencies "
            "to enable video transcription."
        ) from exc

    buf = io.BytesIO()
    with av.open(video_path) as inp:
        audio = next((s for s in inp.streams if s.type == "audio"), None)
        if audio is None:
            raise RuntimeError(f"No audio track found in {video_path!r}")

        with av.open(buf, "w", format="wav") as out:
            out_stream = out.add_stream("pcm_s16le", rate=16_000, layout="mono")
            resampler = av.AudioResampler("s16", "mono", 16_000)

            for frame in inp.decode(audio):
                for rframe in resampler.resample(frame):
                    rframe.pts = None
                    for pkt in out_stream.encode(rframe):
                        out.mux(pkt)

            # Flush resampler
            for rframe in resampler.resample(None):
                rframe.pts = None
                for pkt in out_stream.encode(rframe):
                    out.mux(pkt)

            # Flush encoder
            for pkt in out_stream.encode(None):
                out.mux(pkt)

    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Duration / timestamp helpers
# ---------------------------------------------------------------------------

_ISO_DUR = re.compile(
    r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", re.ASCII
)


def _iso_to_seconds(d: str) -> float:
    """Convert an ISO 8601 duration string (e.g. ``PT1M30.5S``) to seconds."""
    m = _ISO_DUR.match(d or "")
    if not m:
        return 0.0
    days, hours, minutes, seconds = (float(g or 0) for g in m.groups())
    return days * 86_400 + hours * 3_600 + minutes * 60 + seconds


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


# ---------------------------------------------------------------------------
# Phrase → Block conversion
# ---------------------------------------------------------------------------

def _phrases_to_blocks(phrases: list[dict], segment_seconds: int) -> list[Block]:
    """Group timestamped phrases into time-windowed TEXT blocks."""
    blocks: list[Block] = []
    order = 0
    window_start = 0.0
    window_texts: list[str] = []

    for phrase in phrases:
        text = (phrase.get("text") or "").strip()
        if not text:
            continue
        offset = _iso_to_seconds(phrase.get("offset", "PT0S"))

        if window_texts and offset >= window_start + segment_seconds:
            blocks.append(Block(
                kind=BlockKind.TEXT,
                order=order,
                text=f"[{_fmt_ts(window_start)}–{_fmt_ts(offset)}]\n" + " ".join(window_texts),
            ))
            order += 1
            window_start = offset
            window_texts = []

        window_texts.append(text)

    if window_texts:
        blocks.append(Block(
            kind=BlockKind.TEXT,
            order=order,
            text=f"[{_fmt_ts(window_start)}]\n" + " ".join(window_texts),
        ))

    return blocks


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SpeechTranscriptionClient:
    """Transcribes video files via the Azure Speech Fast Transcription API.

    Drop-in replacement for ``ContentUnderstandingClient``:
      - same ``enabled`` property
      - same ``extract_blocks(file_path, ext) -> list[Block]`` method
      - same ``close()`` for HTTP connection cleanup
    """

    def __init__(
        self,
        config: SpeechTranscriptionConfig,
        credential: DefaultAzureCredential | None = None,
    ):
        self._cfg = config
        self._credential = credential or DefaultAzureCredential()
        self._endpoint = (config.endpoint or "").rstrip("/")
        self._http = httpx.Client(timeout=300.0)
        self._token: str | None = None
        self._token_expires_on: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.endpoint)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_blocks(self, file_path: str, ext: str) -> list[Block]:
        """Transcribe a video file and return ordered TEXT blocks.

        Returns an empty list when not configured, the extension is
        unsupported, or any step in the pipeline fails.
        """
        if not self.enabled:
            return []
        if ext.lower() not in VIDEO_SUPPORTED_EXTS:
            return []

        # Step 1 — audio extraction
        try:
            wav_bytes = _extract_audio_wav(file_path)
        except ImportError as exc:
            logger.warning("%s", exc)
            return []
        except RuntimeError as exc:
            logger.warning("Audio extraction failed for %s: %s", file_path, exc)
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error extracting audio from %s: %s", file_path, exc)
            return []

        # Step 2 — transcription
        try:
            phrases = self._transcribe(wav_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Speech transcription failed for %s: %s", file_path, exc)
            return []

        blocks = _phrases_to_blocks(phrases, self._cfg.segment_seconds)
        logger.info("Speech transcription: %d block(s) from %s", len(blocks), file_path)
        return blocks

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _bearer(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_on - 60:
            return self._token
        token = self._credential.get_token(_SCOPE)
        self._token = token.token
        self._token_expires_on = float(token.expires_on)
        return self._token

    def _transcribe(self, wav_bytes: bytes) -> list[dict]:
        """POST WAV to the Fast Transcription endpoint; return phrases list."""
        url = (
            f"{self._endpoint}/speechtotext/transcriptions:transcribe"
            f"?api-version={self._cfg.api_version}"
        )
        definition = json.dumps({
            "locales": [self._cfg.locale],
            "profanityFilterMode": "None",
        })
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="definition"\r\n'
            f"Content-Type: application/json\r\n\r\n"
            f"{definition}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="audio"; filename="audio.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + wav_bytes + f"\r\n--{boundary}--\r\n".encode()

        resp = self._http.post(
            url,
            content=body,
            headers={
                "Authorization": f"Bearer {self._bearer()}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        resp.raise_for_status()
        return resp.json().get("phrases", [])

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass
