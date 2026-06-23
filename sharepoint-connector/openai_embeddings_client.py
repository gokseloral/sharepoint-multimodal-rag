"""
Azure OpenAI embeddings + GPT-4o image-captioning client.

Use this instead of (or in preference to) the Azure AI Vision Florence client
when deploying to regions where Florence multimodal 4.0 is not available
(e.g. Canada Central).

Embedding (text-embedding-3-large, 3072d):
  POST {endpoint}/openai/deployments/{embedding_model}/embeddings
  Body: {"input": "<text>"}
  Response: data[0].embedding → list[float]

Image captioning (gpt-4o, optional):
  POST {endpoint}/openai/deployments/{vision_model}/chat/completions
  Body: vision chat with base64 image data URL
  Response: choices[0].message.content → caption text
  → caption (+ neighbour_text context) is then embedded with the embedding model

When vision_model is empty, vectorize_image falls back to embedding the
neighbour_text (text surrounding a figure, provided by Document Intelligence
Layout). For standalone images with no surrounding text and no vision model
configured the chunk is skipped — log a warning.

Auth: DefaultAzureCredential → bearer token for scope
      https://cognitiveservices.azure.com/.default
      (Cognitive Services User RBAC role on the AI Services / Foundry account;
       same role already assigned by the Bicep template.)

Public interface is identical to MultimodalEmbeddingsClient so the indexer
uses either client interchangeably via duck typing.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time

import httpx
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

_SCOPE = "https://cognitiveservices.azure.com/.default"
_DEFAULT_API_VERSION = "2024-12-01-preview"

_CAPTION_SYSTEM = (
    "You are an assistant that describes images for a document search index. "
    "Return a concise, factual description (3–10 sentences) covering all text "
    "visible in the image, chart labels, diagram elements, table contents, and "
    "the overall subject. Do not speculate about information not visible."
)
_CAPTION_USER = "Describe this image in detail so it can be found by a text search."


class OpenAIEmbeddingsClient:
    """Azure OpenAI embedding + optional GPT-4o vision captioning client.

    Public interface mirrors MultimodalEmbeddingsClient:
      vectorize_text(text)                              → list[float] | None
      vectorize_image(image_bytes, mime, neighbour_text) → list[float] | None
      close()
    """

    def __init__(
        self,
        endpoint: str,
        embedding_model: str,
        vision_model: str = "",
        api_version: str = _DEFAULT_API_VERSION,
        credential: DefaultAzureCredential | None = None,
        max_concurrency: int | None = None,
    ):
        if not endpoint:
            raise ValueError("OpenAIEmbeddingsClient requires an endpoint")
        if not embedding_model:
            raise ValueError("OpenAIEmbeddingsClient requires an embedding_model")

        self._endpoint = endpoint.rstrip("/")
        self._embedding_model = embedding_model
        self._vision_model = vision_model
        self._api_version = api_version
        self._credential = credential or DefaultAzureCredential()
        self._http = httpx.Client(timeout=60.0)

        self._token: str | None = None
        self._token_expires_on: float = 0.0

        if max_concurrency is None:
            max_concurrency = int(os.getenv("MULTIMODAL_MAX_IN_FLIGHT", "8"))
        self._semaphore = threading.BoundedSemaphore(max_concurrency)

        # Global cool-off: a 429 on any thread backs off all threads.
        self._cool_off_until_lock = threading.Lock()
        self._cool_off_until = 0.0

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

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer()}",
            "Content-Type": "application/json",
        }

    def _url(self, deployment: str, operation: str) -> str:
        return (
            f"{self._endpoint}/openai/deployments/{deployment}/{operation}"
            f"?api-version={self._api_version}"
        )

    # ------------------------------------------------------------------ #
    # Rate-limit cool-off  (same pattern as MultimodalEmbeddingsClient)
    # ------------------------------------------------------------------ #

    def _wait_for_cool_off(self) -> None:
        with self._cool_off_until_lock:
            deadline = self._cool_off_until
        now = time.monotonic()
        if deadline > now:
            time.sleep(deadline - now)

    def _set_cool_off(self, seconds: float) -> None:
        seconds = max(1.0, min(seconds, 120.0))
        with self._cool_off_until_lock:
            proposed = time.monotonic() + seconds
            if proposed > self._cool_off_until:
                self._cool_off_until = proposed

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def vectorize_text(self, text: str) -> list[float] | None:
        """Embed a text string. Returns None on error."""
        if not text or not text.strip():
            return None
        body = {"input": text}
        return self._post_json(
            self._url(self._embedding_model, "embeddings"),
            body,
            _extract_embedding,
        )

    def vectorize_image(
        self,
        image_bytes: bytes,
        mime: str = "image/png",
        neighbour_text: str = "",
    ) -> list[float] | None:
        """Caption image with GPT-4o (if configured) then embed the result.

        Strategy:
          1. If vision_model is set: call GPT-4o to produce a text caption.
          2. Combine caption + neighbour_text (DocIntel context around the figure).
          3. Embed the combined text.
        Falls back to embedding neighbour_text alone when vision_model is empty.
        Returns None (and logs a warning) when there is neither caption nor
        neighbour text.
        """
        if not image_bytes:
            return None

        caption = ""
        if self._vision_model:
            caption = self._caption_image(image_bytes, mime) or ""
            if not caption:
                logger.warning(
                    "GPT-4o image captioning returned empty; falling back to neighbour_text"
                )

        combined = "\n\n".join(part for part in (caption, neighbour_text) if part.strip())
        if not combined:
            logger.warning(
                "Skipping image vector: no caption produced and no neighbour_text available. "
                "Set AZURE_OPENAI_VISION_MODEL to enable GPT-4o captioning."
            )
            return None

        return self.vectorize_text(combined)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ #
    # GPT-4o image captioning
    # ------------------------------------------------------------------ #

    def _caption_image(self, image_bytes: bytes, mime: str) -> str:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        body = {
            "messages": [
                {"role": "system", "content": _CAPTION_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                        {"type": "text", "text": _CAPTION_USER},
                    ],
                },
            ],
            "max_completion_tokens": 2048,
        }
        result = self._post_json(
            self._url(self._vision_model, "chat/completions"),
            body,
            _extract_chat_text,
        )
        return result or ""

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    def _post_json(self, url: str, body: dict, extractor, max_retries: int = 5):
        with self._semaphore:
            for attempt in range(max_retries):
                self._wait_for_cool_off()
                try:
                    resp = self._http.post(url, headers=self._headers(), json=body)
                except httpx.HTTPError as e:
                    wait = min(2 ** attempt, 30)
                    logger.warning(f"OpenAI transient error: {e}. Retrying in {wait}s")
                    time.sleep(wait)
                    continue

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    self._set_cool_off(retry_after)
                    logger.warning(
                        f"OpenAI rate-limited (429); cool-off {retry_after:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    continue
                if resp.status_code >= 500:
                    wait = min(2 ** attempt, 30)
                    logger.warning(
                        f"OpenAI server error {resp.status_code}; retrying in {wait}s"
                    )
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    logger.error(f"OpenAI error {resp.status_code}: {resp.text[:500]}")
                    return None

                try:
                    return extractor(resp.json())
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"OpenAI response parse error: {e}")
                    return None

        logger.error(f"OpenAI request exhausted retries: {url}")
        return None


# ------------------------------------------------------------------ #
# Response extractors (module-level for testability)
# ------------------------------------------------------------------ #

def _extract_embedding(data: dict) -> list[float] | None:
    try:
        return data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError):
        logger.warning(f"Embedding response missing expected fields: {list(data)}")
        return None


def _extract_chat_text(data: dict) -> str | None:
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning(f"Chat completion response missing expected fields: {list(data)}")
        return None
