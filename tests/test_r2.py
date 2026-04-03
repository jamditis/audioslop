import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import will fail until r2.py is implemented -- that's expected at first
import r2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_env(account_id="acct123", access_key="access456", secret_key="secret789"):
    return {
        "R2_ACCOUNT_ID": account_id,
        "R2_ACCESS_KEY": access_key,
        "R2_SECRET_KEY": secret_key,
    }


# ---------------------------------------------------------------------------
# get_client
# ---------------------------------------------------------------------------

class TestGetClient:
    def setup_method(self):
        # Reset cached client before each test
        r2._client = None

    def test_get_client_creates_boto3_client(self):
        mock_client = MagicMock()
        with patch.dict(os.environ, _make_env()):
            with patch("boto3.client", return_value=mock_client) as mock_boto3:
                client = r2.get_client()

        mock_boto3.assert_called_once_with(
            "s3",
            endpoint_url="https://acct123.r2.cloudflarestorage.com",
            aws_access_key_id="access456",
            aws_secret_access_key="secret789",
            region_name="auto",
        )
        assert client is mock_client

    def test_get_client_caches_client(self):
        mock_client = MagicMock()
        with patch.dict(os.environ, _make_env()):
            with patch("boto3.client", return_value=mock_client) as mock_boto3:
                c1 = r2.get_client()
                c2 = r2.get_client()

        # boto3.client should only be called once
        assert mock_boto3.call_count == 1
        assert c1 is c2

    def test_get_client_explicit_params_override_env(self):
        mock_client = MagicMock()
        env = _make_env(account_id="env_acct", access_key="env_access", secret_key="env_secret")
        with patch.dict(os.environ, env):
            with patch("boto3.client", return_value=mock_client) as mock_boto3:
                r2.get_client(
                    account_id="override_acct",
                    access_key="override_access",
                    secret_key="override_secret",
                )

        mock_boto3.assert_called_once_with(
            "s3",
            endpoint_url="https://override_acct.r2.cloudflarestorage.com",
            aws_access_key_id="override_access",
            aws_secret_access_key="override_secret",
            region_name="auto",
        )


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------

class TestUploadFile:
    def setup_method(self):
        r2._client = None

    def test_upload_file_calls_s3(self, tmp_path):
        test_file = tmp_path / "test.wav"
        test_file.write_bytes(b"audio data")

        mock_client = MagicMock()
        r2._client = mock_client

        r2.upload_file(str(test_file), "jobs/abc123/audio/seg_000.wav")

        mock_client.upload_file.assert_called_once_with(
            str(test_file),
            "audioslop",
            "jobs/abc123/audio/seg_000.wav",
        )

    def test_upload_file_returns_r2_key(self, tmp_path):
        test_file = tmp_path / "seg.wav"
        test_file.write_bytes(b"data")

        mock_client = MagicMock()
        r2._client = mock_client

        result = r2.upload_file(str(test_file), "jobs/abc/seg.wav")

        assert result == "jobs/abc/seg.wav"


# ---------------------------------------------------------------------------
# presigned_url
# ---------------------------------------------------------------------------

class TestPresignedUrl:
    def setup_method(self):
        r2._client = None

    def test_presigned_url(self):
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = (
            "https://acct123.r2.cloudflarestorage.com/audioslop/jobs/abc/seg.wav?X-Amz-Signature=abc"
        )
        r2._client = mock_client

        url = r2.presigned_url("jobs/abc/seg.wav")

        mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "audioslop", "Key": "jobs/abc/seg.wav"},
            ExpiresIn=3600,
        )
        assert url.startswith("https://")
        assert "seg.wav" in url

    def test_presigned_url_custom_expiry(self):
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://example.com/signed"
        r2._client = mock_client

        r2.presigned_url("some/key.wav", expires_in=300)

        mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "audioslop", "Key": "some/key.wav"},
            ExpiresIn=300,
        )


# ---------------------------------------------------------------------------
# delete_prefix
# ---------------------------------------------------------------------------

class TestDeletePrefix:
    def setup_method(self):
        r2._client = None

    def test_delete_prefix(self):
        mock_client = MagicMock()

        # Simulate paginator returning two pages of objects
        page1 = {
            "Contents": [
                {"Key": "jobs/abc/audio/seg_000.wav"},
                {"Key": "jobs/abc/audio/seg_001.wav"},
            ]
        }
        page2 = {
            "Contents": [
                {"Key": "jobs/abc/audio/full.wav"},
            ]
        }
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [page1, page2]
        mock_client.get_paginator.return_value = mock_paginator

        r2._client = mock_client

        deleted = r2.delete_prefix("jobs/abc/audio/")

        mock_client.get_paginator.assert_called_once_with("list_objects_v2")
        mock_paginator.paginate.assert_called_once_with(Bucket="audioslop", Prefix="jobs/abc/audio/")

        # delete_objects should be called once per page with the correct keys
        assert mock_client.delete_objects.call_count == 2
        first_call_objects = mock_client.delete_objects.call_args_list[0][1]["Delete"]["Objects"]
        second_call_objects = mock_client.delete_objects.call_args_list[1][1]["Delete"]["Objects"]
        assert {"Key": "jobs/abc/audio/seg_000.wav"} in first_call_objects
        assert {"Key": "jobs/abc/audio/seg_001.wav"} in first_call_objects
        assert {"Key": "jobs/abc/audio/full.wav"} in second_call_objects

        # Returns count of deleted objects
        assert deleted == 3

    def test_delete_prefix_empty(self):
        mock_client = MagicMock()

        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{}]  # page with no Contents key
        mock_client.get_paginator.return_value = mock_paginator

        r2._client = mock_client

        deleted = r2.delete_prefix("jobs/nonexistent/")

        mock_client.delete_objects.assert_not_called()
        assert deleted == 0
