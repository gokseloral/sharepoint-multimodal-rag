"""Tests for metadata-filter support in sharepoint_client.py.

These tests verify:
  - _matches_metadata_filter() helper (module-level function)
  - list_files() adds $expand when filter is enabled, and filters results
  - list_all_files() passes metadata_filter through to list_files()
  - list_changes_via_delta() fetches per-item fields and filters in the delta path
  - list_changes_all_drives() passes metadata_filter to list_changes_via_delta()
"""

import pytest
from unittest.mock import MagicMock, patch, call
from config import MetadataFilterConfig
from sharepoint_client import SharePointClient, _matches_metadata_filter


# ---------------------------------------------------------------------------
# _matches_metadata_filter helper
# ---------------------------------------------------------------------------

class TestMatchesMetadataFilter:
    def _mf(self, *pairs):
        return MetadataFilterConfig(filters=tuple(pairs))

    def test_single_filter_match(self):
        fields = {"DocumentStatusTX": "Approved"}
        assert _matches_metadata_filter(fields, self._mf(("DocumentStatusTX", "Approved"))) is True

    def test_single_filter_no_match(self):
        fields = {"DocumentStatusTX": "Draft"}
        assert _matches_metadata_filter(fields, self._mf(("DocumentStatusTX", "Approved"))) is False

    def test_case_insensitive_value_match(self):
        fields = {"DocumentStatusTX": "approved"}
        assert _matches_metadata_filter(fields, self._mf(("DocumentStatusTX", "Approved"))) is True

    def test_case_insensitive_uppercase_expected(self):
        fields = {"DocumentStatusTX": "APPROVED"}
        assert _matches_metadata_filter(fields, self._mf(("DocumentStatusTX", "Approved"))) is True

    def test_missing_column_returns_false(self):
        fields = {}
        assert _matches_metadata_filter(fields, self._mf(("DocumentStatusTX", "Approved"))) is False

    def test_multiple_filters_all_match(self):
        fields = {"DocumentStatusTX": "Approved", "DocumentTypeTX": "Guideline"}
        mf = self._mf(("DocumentStatusTX", "Approved"), ("DocumentTypeTX", "Guideline"))
        assert _matches_metadata_filter(fields, mf) is True

    def test_multiple_filters_one_mismatch(self):
        fields = {"DocumentStatusTX": "Approved", "DocumentTypeTX": "Policy"}
        mf = self._mf(("DocumentStatusTX", "Approved"), ("DocumentTypeTX", "Guideline"))
        assert _matches_metadata_filter(fields, mf) is False

    def test_multiple_filters_all_mismatch(self):
        fields = {"DocumentStatusTX": "Draft", "DocumentTypeTX": "Policy"}
        mf = self._mf(("DocumentStatusTX", "Approved"), ("DocumentTypeTX", "Guideline"))
        assert _matches_metadata_filter(fields, mf) is False

    def test_extra_columns_in_fields_are_ignored(self):
        """Extra fields in the response don't cause a failure."""
        fields = {"DocumentStatusTX": "Approved", "UnrelatedField": "Noise"}
        assert _matches_metadata_filter(fields, self._mf(("DocumentStatusTX", "Approved"))) is True

    def test_numeric_value_coerced_to_string(self):
        """Numeric field values are str()-coerced before comparison."""
        fields = {"Year": 2024}
        assert _matches_metadata_filter(fields, self._mf(("Year", "2024"))) is True

    def test_empty_filter_list_always_true(self):
        mf = MetadataFilterConfig(filters=())
        assert _matches_metadata_filter({}, mf) is True


# ---------------------------------------------------------------------------
# Shared test fixture helpers
# ---------------------------------------------------------------------------

def _make_sp_client():
    """Return a SharePointClient whose credential and HTTP are mocked."""
    from config import EntraConfig, SharePointConfig
    entra = EntraConfig(tenant_id="t", client_id="", client_secret="")
    sp_cfg = SharePointConfig(site_url="https://contoso.sharepoint.com/sites/Test")
    with patch("sharepoint_client.DefaultAzureCredential"):
        client = SharePointClient(entra, sp_cfg)
    # Replace HTTP client with a mock so no real requests are made
    client._http = MagicMock()
    # Stub token so _headers() never calls the real credential
    client._get_token = MagicMock(return_value="mock-token")
    return client


def _make_file_item(name="doc.pdf", item_id="item1", fields=None):
    """Build a minimal Graph drive item dict with optional listItem.fields."""
    item = {
        "id": item_id,
        "name": name,
        "file": {},
        "size": 1024,
        "lastModifiedDateTime": "2024-01-01T00:00:00Z",
        "webUrl": f"https://contoso.sharepoint.com/{name}",
        "createdBy": {"user": {"displayName": ""}},
        "lastModifiedBy": {"user": {"displayName": ""}},
        "parentReference": {"driveId": "drive1", "path": "root:"},
    }
    if fields is not None:
        item["listItem"] = {"fields": fields}
    return item


# ---------------------------------------------------------------------------
# list_files with metadata_filter
# ---------------------------------------------------------------------------

class TestListFilesMetadataFilter:
    """list_files should request $expand=listItem and filter based on fields."""

    def _patch_get_all_pages(self, client, items):
        client._get_all_pages = MagicMock(return_value=items)

    def test_no_filter_no_expand(self):
        """When metadata_filter is None, URL has no $expand param."""
        client = _make_sp_client()
        self._patch_get_all_pages(client, [])

        client.list_files("drive1", metadata_filter=None)

        url_used = client._get_all_pages.call_args[0][0]
        assert "$expand" not in url_used

    def test_filter_adds_expand_to_url(self):
        """When metadata_filter is enabled, $expand is added to the URL."""
        client = _make_sp_client()
        self._patch_get_all_pages(client, [])
        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))

        client.list_files("drive1", metadata_filter=mf)

        url_used = client._get_all_pages.call_args[0][0]
        assert "$expand=listItem" in url_used
        assert "DocumentStatusTX" in url_used

    def test_filter_includes_all_columns(self):
        """All filter columns appear in the $select inside $expand."""
        client = _make_sp_client()
        self._patch_get_all_pages(client, [])
        mf = MetadataFilterConfig(filters=(
            ("DocumentStatusTX", "Approved"),
            ("DocumentTypeTX", "Guideline"),
        ))

        client.list_files("drive1", metadata_filter=mf)

        url_used = client._get_all_pages.call_args[0][0]
        assert "DocumentStatusTX" in url_used
        assert "DocumentTypeTX" in url_used

    def test_matching_file_is_included(self):
        item = _make_file_item("report.pdf", fields={"DocumentStatusTX": "Approved"})
        client = _make_sp_client()
        self._patch_get_all_pages(client, [item])
        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))

        result = client.list_files("drive1", metadata_filter=mf)

        assert len(result) == 1
        assert result[0]["name"] == "report.pdf"

    def test_non_matching_file_is_excluded(self):
        item = _make_file_item("draft.pdf", fields={"DocumentStatusTX": "Draft"})
        client = _make_sp_client()
        self._patch_get_all_pages(client, [item])
        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))

        result = client.list_files("drive1", metadata_filter=mf)

        assert result == []

    def test_missing_list_item_fields_excluded(self):
        """Items without listItem.fields don't pass the filter."""
        item = _make_file_item("nodoc.pdf")  # no listItem key
        client = _make_sp_client()
        self._patch_get_all_pages(client, [item])
        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))

        result = client.list_files("drive1", metadata_filter=mf)

        assert result == []

    def test_case_insensitive_filter(self):
        item = _make_file_item("ok.pdf", fields={"DocumentStatusTX": "APPROVED"})
        client = _make_sp_client()
        self._patch_get_all_pages(client, [item])
        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))

        result = client.list_files("drive1", metadata_filter=mf)

        assert len(result) == 1

    def test_multiple_items_mixed_results(self):
        items = [
            _make_file_item("approved.pdf", "i1", {"DocumentStatusTX": "Approved"}),
            _make_file_item("draft.pdf", "i2", {"DocumentStatusTX": "Draft"}),
            _make_file_item("also_approved.docx", "i3", {"DocumentStatusTX": "Approved"}),
        ]
        client = _make_sp_client()
        self._patch_get_all_pages(client, items)
        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))

        result = client.list_files("drive1", metadata_filter=mf)

        names = [r["name"] for r in result]
        assert "approved.pdf" in names
        assert "also_approved.docx" in names
        assert "draft.pdf" not in names


# ---------------------------------------------------------------------------
# list_all_files passes metadata_filter to list_files
# ---------------------------------------------------------------------------

class TestListAllFilesPassesFilter:
    def test_metadata_filter_forwarded(self):
        client = _make_sp_client()
        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))
        client.get_target_drives = MagicMock(return_value=[
            {"id": "drive1", "name": "Documents", "webUrl": "https://x"}
        ])
        client.list_files = MagicMock(return_value=[])

        client.list_all_files(metadata_filter=mf)

        client.list_files.assert_called_once()
        # metadata_filter is the 5th positional arg in list_files call
        positional = client.list_files.call_args[0]
        assert positional[4] is mf

    def test_no_filter_forwarded_as_none(self):
        client = _make_sp_client()
        client.get_target_drives = MagicMock(return_value=[
            {"id": "drive1", "name": "Documents", "webUrl": "https://x"}
        ])
        client.list_files = MagicMock(return_value=[])

        client.list_all_files(metadata_filter=None)

        client.list_files.assert_called_once()
        positional = client.list_files.call_args[0]
        assert positional[4] is None


# ---------------------------------------------------------------------------
# list_changes_via_delta — per-item field fetch
# ---------------------------------------------------------------------------

class TestDeltaMetadataFilter:
    """Delta path fetches per-item fields and applies the metadata filter."""

    def _setup_delta(self, client, delta_items):
        """Patch _get to return a single delta page, ending the pagination loop."""
        response = {
            "value": delta_items,
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token=abc",
        }
        client._get = MagicMock(return_value=response)

    def test_matching_item_passes_filter(self):
        client = _make_sp_client()
        item = _make_file_item("report.pdf", "item1")
        self._setup_delta(client, [item])

        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))
        client._get_list_item_fields = MagicMock(
            return_value={"DocumentStatusTX": "Approved"}
        )

        modified, deleted, token = client.list_changes_via_delta("drive1", None, metadata_filter=mf)

        assert len(modified) == 1
        assert modified[0]["name"] == "report.pdf"
        client._get_list_item_fields.assert_called_once_with(
            "drive1", "item1", "DocumentStatusTX"
        )

    def test_non_matching_item_is_excluded(self):
        client = _make_sp_client()
        item = _make_file_item("draft.pdf", "item2")
        self._setup_delta(client, [item])

        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))
        client._get_list_item_fields = MagicMock(
            return_value={"DocumentStatusTX": "Draft"}
        )

        modified, deleted, token = client.list_changes_via_delta("drive1", None, metadata_filter=mf)

        assert modified == []

    def test_no_filter_skips_field_fetch(self):
        """When metadata_filter is None, _get_list_item_fields is never called."""
        client = _make_sp_client()
        item = _make_file_item("doc.pdf", "item3")
        self._setup_delta(client, [item])
        client._get_list_item_fields = MagicMock()

        client.list_changes_via_delta("drive1", None, metadata_filter=None)

        client._get_list_item_fields.assert_not_called()

    def test_field_fetch_error_excludes_item(self):
        """If _get_list_item_fields returns {} (error), item is excluded (fields don't match)."""
        client = _make_sp_client()
        item = _make_file_item("broken.pdf", "item4")
        self._setup_delta(client, [item])

        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))
        # _get_list_item_fields returns empty dict (simulates network error)
        client._get_list_item_fields = MagicMock(return_value={})

        modified, deleted, token = client.list_changes_via_delta("drive1", None, metadata_filter=mf)

        assert modified == []

    def test_deleted_items_not_filtered(self):
        """Deleted items (no 'file' key) are never passed through the metadata filter."""
        client = _make_sp_client()
        deleted_item = {"id": "del1", "deleted": {}}
        self._setup_delta(client, [deleted_item])
        client._get_list_item_fields = MagicMock()

        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))
        modified, deleted, token = client.list_changes_via_delta("drive1", None, metadata_filter=mf)

        assert "del1" in deleted
        client._get_list_item_fields.assert_not_called()

    def test_multiple_columns_passed_to_field_fetch(self):
        """Multiple filter columns are all included in the $select query."""
        client = _make_sp_client()
        item = _make_file_item("multi.pdf", "item5")
        self._setup_delta(client, [item])

        mf = MetadataFilterConfig(filters=(
            ("DocumentStatusTX", "Approved"),
            ("DocumentTypeTX", "Guideline"),
        ))
        client._get_list_item_fields = MagicMock(
            return_value={"DocumentStatusTX": "Approved", "DocumentTypeTX": "Guideline"}
        )

        modified, _, _ = client.list_changes_via_delta("drive1", None, metadata_filter=mf)

        # Both columns should be in the $select string
        call_args = client._get_list_item_fields.call_args[0]
        columns_arg = call_args[2]  # third positional arg is the columns string
        assert "DocumentStatusTX" in columns_arg
        assert "DocumentTypeTX" in columns_arg
        assert len(modified) == 1


# ---------------------------------------------------------------------------
# list_changes_all_drives passes metadata_filter
# ---------------------------------------------------------------------------

class TestListChangesAllDrivesFilter:
    def test_metadata_filter_forwarded_to_per_drive_call(self):
        client = _make_sp_client()
        mf = MetadataFilterConfig(filters=(("DocumentStatusTX", "Approved"),))
        client.get_target_drives = MagicMock(return_value=[
            {"id": "drive1", "name": "Documents"}
        ])
        client.list_changes_via_delta = MagicMock(return_value=([], [], "delta-token"))

        client.list_changes_all_drives(metadata_filter=mf)

        client.list_changes_via_delta.assert_called_once()
        # list_changes_via_delta is called positionally: (drive_id, token, extensions, metadata_filter)
        positional = client.list_changes_via_delta.call_args[0]
        assert positional[3] is mf

    def test_no_filter_forwarded_as_none(self):
        client = _make_sp_client()
        client.get_target_drives = MagicMock(return_value=[
            {"id": "drive1", "name": "Documents"}
        ])
        client.list_changes_via_delta = MagicMock(return_value=([], [], "delta-token"))

        client.list_changes_all_drives(metadata_filter=None)

        client.list_changes_via_delta.assert_called_once()
        positional = client.list_changes_via_delta.call_args[0]
        assert positional[3] is None
