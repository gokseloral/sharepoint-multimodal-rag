"""Tests for config.py — configuration loading and validation."""

import os
import pytest
from unittest.mock import patch

# Import at module level so load_dotenv() runs BEFORE any env patching
from config import FunctionProcessingMode, MetadataFilterConfig, ProcessingMode, load_config

# Minimal required env vars for a valid config — using Azure OpenAI (recommended path)
_REQUIRED_ENV = {
    "TENANT_ID": "test-tenant-id",
    "SHAREPOINT_SITE_URL": "https://contoso.sharepoint.com/sites/TestSite",
    "SEARCH_ENDPOINT": "https://my-search.search.windows.net",
    "AZURE_OPENAI_ENDPOINT": "https://my-foundry.cognitiveservices.azure.com",
}

# Florence-only env vars (legacy regions with Florence but no OpenAI quota)
_FLORENCE_ENV = {
    "TENANT_ID": "test-tenant-id",
    "SHAREPOINT_SITE_URL": "https://contoso.sharepoint.com/sites/TestSite",
    "SEARCH_ENDPOINT": "https://my-search.search.windows.net",
    "MULTIMODAL_ENDPOINT": "https://my-foundry.cognitiveservices.azure.com",
}


class TestLoadConfigRequired:
    """Test that missing required vars raise EnvironmentError."""

    def test_missing_tenant_id(self):
        env = {k: v for k, v in _REQUIRED_ENV.items() if k != "TENANT_ID"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(EnvironmentError, match="TENANT_ID"):
                load_config()

    def test_missing_sharepoint_site_url(self):
        env = {k: v for k, v in _REQUIRED_ENV.items() if k != "SHAREPOINT_SITE_URL"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(EnvironmentError, match="SHAREPOINT_SITE_URL"):
                load_config()

    def test_missing_search_endpoint(self):
        env = {k: v for k, v in _REQUIRED_ENV.items() if k != "SEARCH_ENDPOINT"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(EnvironmentError, match="SEARCH_ENDPOINT"):
                load_config()

    def test_missing_multimodal_endpoint(self):
        """Neither AZURE_OPENAI_ENDPOINT nor MULTIMODAL_ENDPOINT set → error."""
        env = {k: v for k, v in _REQUIRED_ENV.items() if k != "AZURE_OPENAI_ENDPOINT"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(EnvironmentError, match="AZURE_OPENAI_ENDPOINT"):
                load_config()


class TestLoadConfigDefaults:
    """Test default values when optional vars are not set."""

    def test_defaults(self):
        with patch.dict(os.environ, _REQUIRED_ENV, clear=True):
            cfg = load_config()

        assert cfg.entra.tenant_id == "test-tenant-id"
        assert cfg.entra.client_id == ""
        assert cfg.entra.client_secret == ""
        assert cfg.entra.use_managed_identity is True

        assert cfg.sharepoint.site_url == "https://contoso.sharepoint.com/sites/TestSite"
        assert cfg.sharepoint.libraries == []

        assert cfg.search.index_name == "sharepoint-index"

        assert cfg.multimodal.endpoint == ""
        assert cfg.multimodal.model_version == "2023-04-15"
        assert cfg.multimodal.images_container == "images"

        assert cfg.azure_openai.endpoint == "https://my-foundry.cognitiveservices.azure.com"
        assert cfg.azure_openai.embedding_model == "text-embedding-3-large"
        assert cfg.azure_openai.embedding_dimensions == 3072
        assert cfg.azure_openai.vision_model == "gpt-4o"
        assert cfg.azure_openai.enabled is True

        assert cfg.metadata_filter.enabled is False
        assert cfg.metadata_filter.filters == ()

        assert cfg.indexer.chunk_size == 2000
        assert cfg.indexer.chunk_overlap == 200
        assert cfg.indexer.max_concurrency == 4
        assert cfg.indexer.max_file_size_mb == 500
        assert cfg.indexer.processing_mode == ProcessingMode.SINCE_LAST_RUN
        assert cfg.indexer.start_date is None
        assert cfg.indexer.function_processing_mode == FunctionProcessingMode.QUEUE
        assert cfg.indexer.extract_images is True

    def test_default_extensions_count(self):
        with patch.dict(os.environ, _REQUIRED_ENV, clear=True):
            cfg = load_config()

        # Default extensions include documents, images, and video formats (38 total).
        assert len(cfg.indexer.indexed_extensions) == 38
        assert ".pdf" in cfg.indexer.indexed_extensions
        assert ".zip" in cfg.indexer.indexed_extensions
        assert ".gz" in cfg.indexer.indexed_extensions
        assert ".png" in cfg.indexer.indexed_extensions
        assert ".jpg" in cfg.indexer.indexed_extensions
        assert ".vsdx" in cfg.indexer.indexed_extensions
        assert ".mp4" in cfg.indexer.indexed_extensions
        assert ".mov" in cfg.indexer.indexed_extensions

class TestLoadConfigFlorencePath:
    """Legacy Florence-only config (no Azure OpenAI endpoint)."""

    def test_florence_path_loads(self):
        with patch.dict(os.environ, _FLORENCE_ENV, clear=True):
            cfg = load_config()
        assert cfg.multimodal.endpoint == "https://my-foundry.cognitiveservices.azure.com"
        assert cfg.multimodal.enabled is True
        assert cfg.azure_openai.enabled is False


class TestLoadConfigCustomValues:
    """Test that custom env vars override defaults."""

    def test_custom_values(self):
        env = {
            **_REQUIRED_ENV,
            "CLIENT_ID": "my-client-id",
            "CLIENT_SECRET": "my-secret",
            "SHAREPOINT_LIBRARIES": "Documents, Reports, Archive",
            "SEARCH_INDEX_NAME": "custom-index",
            "MULTIMODAL_MODEL_VERSION": "2023-04-15",
            "IMAGES_CONTAINER": "custom-images",
            "CHUNK_SIZE": "1500",
            "CHUNK_OVERLAP": "150",
            "MAX_CONCURRENCY": "8",
            "MAX_FILE_SIZE_MB": "50",
            "PROCESSING_MODE": "full",
            "FUNCTION_PROCESSING_MODE": "inline",
            "EXTRACT_IMAGES": "false",
            "INDEXED_EXTENSIONS": ".pdf,.docx,.txt",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()

        assert cfg.entra.client_id == "my-client-id"
        assert cfg.entra.client_secret == "my-secret"
        assert cfg.entra.use_managed_identity is False

        assert cfg.sharepoint.libraries == ["Documents", "Reports", "Archive"]

        assert cfg.search.index_name == "custom-index"

        assert cfg.multimodal.model_version == "2023-04-15"
        assert cfg.multimodal.images_container == "custom-images"

        assert cfg.indexer.chunk_size == 1500
        assert cfg.indexer.chunk_overlap == 150
        assert cfg.indexer.max_concurrency == 8
        assert cfg.indexer.max_file_size_mb == 50
        assert cfg.indexer.processing_mode == ProcessingMode.FULL
        assert cfg.indexer.function_processing_mode == FunctionProcessingMode.INLINE
        assert cfg.indexer.extract_images is False
        assert cfg.indexer.indexed_extensions == [".pdf", ".docx", ".txt"]


class TestSharePointConfigProperties:
    """Test computed properties on SharePointConfig."""

    def test_hostname_extraction(self):
        with patch.dict(os.environ, _REQUIRED_ENV, clear=True):
            cfg = load_config()
        assert cfg.sharepoint.hostname == "contoso.sharepoint.com"

    def test_site_path_extraction(self):
        with patch.dict(os.environ, _REQUIRED_ENV, clear=True):
            cfg = load_config()
        assert cfg.sharepoint.site_path == "/sites/TestSite"

    def test_site_path_strips_trailing_slash(self):
        env = {**_REQUIRED_ENV, "SHAREPOINT_SITE_URL": "https://contoso.sharepoint.com/sites/TestSite/"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.sharepoint.site_path == "/sites/TestSite"


class TestEntraConfigManagedIdentity:
    """Test use_managed_identity logic."""

    def test_managed_identity_when_no_secret(self):
        env = {**_REQUIRED_ENV, "CLIENT_ID": "some-id"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.entra.use_managed_identity is True

    def test_not_managed_identity_when_secret_set(self):
        env = {**_REQUIRED_ENV, "CLIENT_ID": "some-id", "CLIENT_SECRET": "secret"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.entra.use_managed_identity is False


class TestMetadataFilterConfig:
    """Tests for METADATA_FILTERS parsing and MetadataFilterConfig behaviour."""

    def test_empty_env_var_gives_disabled_filter(self):
        env = {**_REQUIRED_ENV, "METADATA_FILTERS": ""}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.metadata_filter.enabled is False
        assert cfg.metadata_filter.filters == ()

    def test_single_filter_parses(self):
        env = {**_REQUIRED_ENV, "METADATA_FILTERS": "DocumentStatusTX=Approved"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.metadata_filter.enabled is True
        assert cfg.metadata_filter.filters == (("DocumentStatusTX", "Approved"),)

    def test_multiple_filters_parse(self):
        env = {**_REQUIRED_ENV, "METADATA_FILTERS": "DocumentStatusTX=Approved,DocumentTypeTX=Guideline"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.metadata_filter.enabled is True
        assert cfg.metadata_filter.filters == (
            ("DocumentStatusTX", "Approved"),
            ("DocumentTypeTX", "Guideline"),
        )

    def test_whitespace_is_stripped(self):
        env = {**_REQUIRED_ENV, "METADATA_FILTERS": " DocumentStatusTX = Approved , DocumentTypeTX = Guideline "}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.metadata_filter.filters == (
            ("DocumentStatusTX", "Approved"),
            ("DocumentTypeTX", "Guideline"),
        )

    def test_malformed_token_without_equals_is_ignored(self):
        """A token with no '=' is silently skipped."""
        env = {**_REQUIRED_ENV, "METADATA_FILTERS": "NoEqualsHere,DocumentStatusTX=Approved"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.metadata_filter.filters == (("DocumentStatusTX", "Approved"),)

    def test_value_with_equals_sign(self):
        """Values can contain '=' characters (only split on first '=')."""
        env = {**_REQUIRED_ENV, "METADATA_FILTERS": "Column=A=B"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.metadata_filter.filters == (("Column", "A=B"),)

    def test_metadata_filter_config_enabled_false_when_empty(self):
        mf = MetadataFilterConfig()
        assert mf.enabled is False

    def test_metadata_filter_config_enabled_true_when_filters_set(self):
        mf = MetadataFilterConfig(filters=(("Col", "Val"),))
        assert mf.enabled is True
