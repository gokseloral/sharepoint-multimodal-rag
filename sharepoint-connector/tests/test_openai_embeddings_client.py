"""Tests for openai_embeddings_client.py — response parsing and image routing."""

import pytest
from openai_embeddings_client import (
    OpenAIEmbeddingsClient,
    _extract_embedding,
    _extract_chat_text,
)


# ------------------------------------------------------------------
# Response extractors (pure functions, no network)
# ------------------------------------------------------------------

class TestExtractEmbedding:
    def test_valid_response(self):
        data = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
        result = _extract_embedding(data)
        assert result == [0.1, 0.2, 0.3]

    def test_missing_data_key(self):
        assert _extract_embedding({}) is None

    def test_empty_data_list(self):
        assert _extract_embedding({"data": []}) is None

    def test_wrong_type(self):
        assert _extract_embedding({"data": "not-a-list"}) is None


class TestExtractChatText:
    def test_valid_response(self):
        data = {
            "choices": [
                {"message": {"content": "A bar chart showing Q3 revenue by region."}}
            ]
        }
        assert _extract_chat_text(data) == "A bar chart showing Q3 revenue by region."

    def test_missing_choices(self):
        assert _extract_chat_text({}) is None

    def test_empty_choices(self):
        assert _extract_chat_text({"choices": []}) is None


# ------------------------------------------------------------------
# Client construction
# ------------------------------------------------------------------

class TestOpenAIEmbeddingsClientInit:
    def test_requires_endpoint(self):
        with pytest.raises(ValueError, match="endpoint"):
            OpenAIEmbeddingsClient(endpoint="", embedding_model="text-embedding-3-large")

    def test_requires_embedding_model(self):
        with pytest.raises(ValueError, match="embedding_model"):
            OpenAIEmbeddingsClient(
                endpoint="https://my.cognitiveservices.azure.com",
                embedding_model="",
            )


# ------------------------------------------------------------------
# vectorize_image routing (no network — uses fake credential)
# ------------------------------------------------------------------

class _FakeCredential:
    """Returns a fixed fake token without calling Azure AD."""
    class _Token:
        token = "fake-token"
        expires_on = 9_999_999_999.0

    def get_token(self, *args, **kwargs):
        return self._Token()


class TestVectorizeImageFallback:
    """Without a vision_model the client embeds neighbour_text instead."""

    def _make_client(self, vision_model=""):
        return OpenAIEmbeddingsClient(
            endpoint="https://fake.cognitiveservices.azure.com",
            embedding_model="text-embedding-3-large",
            vision_model=vision_model,
            credential=_FakeCredential(),
        )

    def test_returns_none_when_no_bytes(self):
        client = self._make_client()
        assert client.vectorize_image(b"", neighbour_text="some text") is None

    def test_empty_bytes_without_vision_skips(self):
        client = self._make_client()
        # No image bytes, no vision model — must return None
        assert client.vectorize_image(b"") is None

    def test_vectorize_text_empty_returns_none(self):
        client = self._make_client()
        assert client.vectorize_text("") is None
        assert client.vectorize_text("   ") is None

    def test_close_does_not_raise(self):
        client = self._make_client()
        client.close()  # should not raise
