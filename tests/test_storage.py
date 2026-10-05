"""Tests for the S3 storage helper.

The credential path is the part that breaks silently in production, so these
cover the configuration that boto3 reads at import time. No AWS call is made:
constructing a client does not contact the network, and asserting on a live
bucket would make the suite depend on credentials that CI does not have.
"""

import importlib
import os
import sys

sys.path.insert(0, os.getcwd())


def _reload(monkeypatch, **env):
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import app.storage as storage

    return importlib.reload(storage)


def test_region_defaults_when_env_missing(monkeypatch):
    storage = _reload(monkeypatch, AWS_REGION=None, AWS_DEFAULT_REGION=None, S3_UPLOAD_BUCKET=None)
    assert storage.REGION == "eu-north-1"


def test_aws_region_wins(monkeypatch):
    storage = _reload(
        monkeypatch,
        AWS_REGION="eu-north-1",
        AWS_DEFAULT_REGION="us-east-1",
        S3_UPLOAD_BUCKET=None,
    )
    assert storage.REGION == "eu-north-1"


def test_default_region_used_when_primary_absent(monkeypatch):
    storage = _reload(
        monkeypatch,
        AWS_REGION=None,
        AWS_DEFAULT_REGION="eu-central-1",
        S3_UPLOAD_BUCKET=None,
    )
    assert storage.REGION == "eu-central-1"


def test_bucket_comes_from_compose(monkeypatch):
    storage = _reload(
        monkeypatch,
        AWS_REGION="eu-north-1",
        S3_UPLOAD_BUCKET="cricketapp-uploads-crizz",
    )
    assert storage.BUCKET == "cricketapp-uploads-crizz"


def test_client_resolves_credentials_from_the_chain(monkeypatch):
    """Credentials must be resolved live, never hard-coded in the client.

    Setting env keys and seeing them come back proves the default chain is what
    is in use: on EC2 it resolves the instance profile, in CI or locally it
    resolves whatever else is configured. A client built with explicit
    aws_access_key_id would ignore the environment and fail this.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "example-token")

    storage = _reload(monkeypatch, AWS_REGION="eu-north-1")
    client = storage.get_client()

    assert client.meta.region_name == "eu-north-1"

    frozen = client._request_signer._credentials
    assert frozen is not None, "expected the credential chain to resolve"
    resolved = frozen.get_frozen_credentials()
    assert resolved.access_key == "AKIAEXAMPLE"
    assert resolved.token == "example-token"


def test_source_passes_no_credentials_argument():
    """Guard against someone adding static keys to get_client later.

    Reads the function source rather than trusting a comment, because the whole
    value of the instance profile is that no long-lived secret exists in this
    repository or its image.
    """
    import inspect

    import app.storage as storage

    source = inspect.getsource(storage.get_client)
    for forbidden in (
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "Config(aws_",
    ):
        assert forbidden not in source, f"get_client must not reference {forbidden}"


def test_object_url_shape(monkeypatch):
    storage = _reload(monkeypatch, AWS_REGION="eu-north-1", S3_UPLOAD_BUCKET="my-bucket")
    url = storage.object_url("players/7.png")
    assert url == "https://my-bucket.s3.eu-north-1.amazonaws.com/players/7.png"
