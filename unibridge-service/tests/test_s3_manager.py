"""Unit tests for S3ConnectionManager."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.models import S3Connection
from app.services.connection_manager import encrypt_password
from app.services.s3_manager import S3ConnectionManager, s3_manager


@pytest.fixture
def fresh_manager():
    """Patch the singleton's internal state for isolated tests."""
    saved_clients = dict(s3_manager._clients)
    saved_configs = dict(s3_manager._configs)
    s3_manager._clients = {}
    s3_manager._configs = {}
    yield s3_manager
    s3_manager._clients = saved_clients
    s3_manager._configs = saved_configs


def _make_conn(alias="t", endpoint=None, bucket=None) -> S3Connection:
    return S3Connection(
        alias=alias,
        endpoint_url=endpoint,
        region="us-east-1",
        access_key_id_encrypted=encrypt_password("AKIA-TEST"),
        secret_access_key_encrypted=encrypt_password("SECRET"),
        default_bucket=bucket,
        use_ssl=True,
    )


def test_singleton_returns_same_instance():
    a = S3ConnectionManager()
    b = S3ConnectionManager()
    assert a is b


@pytest.mark.asyncio
async def test_add_and_remove_connection(fresh_manager):
    fake_client = MagicMock()
    with patch("app.services.s3_manager.boto3.client", return_value=fake_client) as boto:
        conn = _make_conn("alias-a", endpoint="https://s3.example", bucket="bk")
        await fresh_manager.add_connection(conn)

        assert fresh_manager.has_connection("alias-a") is True
        assert "alias-a" in fresh_manager.list_aliases()
        assert fresh_manager.get_client("alias-a") is fake_client
        cfg = fresh_manager.get_config("alias-a")
        assert cfg["endpoint_url"] == "https://s3.example"
        assert cfg["default_bucket"] == "bk"
        assert boto.call_args.kwargs["endpoint_url"] == "https://s3.example"

    await fresh_manager.remove_connection("alias-a")
    assert not fresh_manager.has_connection("alias-a")
    fake_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_add_connection_replaces_existing(fresh_manager):
    first = MagicMock()
    second = MagicMock()
    with patch("app.services.s3_manager.boto3.client", side_effect=[first, second]):
        conn = _make_conn("dup")
        await fresh_manager.add_connection(conn)
        await fresh_manager.add_connection(conn)
    first.close.assert_called_once()
    assert fresh_manager.get_client("dup") is second


@pytest.mark.asyncio
async def test_remove_unknown_alias_is_noop(fresh_manager):
    await fresh_manager.remove_connection("does-not-exist")
    assert "does-not-exist" not in fresh_manager.list_aliases()


def test_get_client_unknown_raises(fresh_manager):
    with pytest.raises(KeyError):
        fresh_manager.get_client("nope")


def test_get_config_default_empty(fresh_manager):
    assert fresh_manager.get_config("nope") == {}


def test_has_connection_false(fresh_manager):
    assert fresh_manager.has_connection("missing") is False


@pytest.mark.asyncio
async def test_test_connection_default_bucket_success(fresh_manager):
    fake = MagicMock()
    fake.head_bucket.return_value = {}
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("ok", bucket="my-bucket"))
    ok, msg = await fresh_manager.test_connection("ok")
    assert ok is True
    assert "successful" in msg.lower()
    fake.head_bucket.assert_called_once_with(Bucket="my-bucket")


@pytest.mark.asyncio
async def test_test_connection_no_default_bucket_uses_list_buckets(fresh_manager):
    fake = MagicMock()
    fake.list_buckets.return_value = {"Buckets": []}
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("listbk"))
    ok, _msg = await fresh_manager.test_connection("listbk")
    assert ok is True
    fake.list_buckets.assert_called_once()


@pytest.mark.asyncio
async def test_test_connection_client_error(fresh_manager):
    fake = MagicMock()
    fake.list_buckets.side_effect = ClientError(
        {"Error": {"Code": "InvalidAccessKey", "Message": "bad"}},
        "ListBuckets",
    )
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("badkey"))
    ok, msg = await fresh_manager.test_connection("badkey")
    assert ok is False
    assert "InvalidAccessKey" in msg


@pytest.mark.asyncio
async def test_test_connection_client_error_no_code(fresh_manager):
    fake = MagicMock()
    fake.list_buckets.side_effect = ClientError(
        {"Error": {}},
        "ListBuckets",
    )
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("nocode"))
    ok, msg = await fresh_manager.test_connection("nocode")
    assert ok is False
    assert msg == "Connection failed"


@pytest.mark.asyncio
async def test_test_connection_other_exception(fresh_manager):
    fake = MagicMock()
    fake.list_buckets.side_effect = RuntimeError("network gone")
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("rtfail"))
    ok, msg = await fresh_manager.test_connection("rtfail")
    assert ok is False
    assert msg == "Connection failed"


@pytest.mark.asyncio
async def test_list_buckets_returns_normalized(fresh_manager):
    fake = MagicMock()
    fake.list_buckets.return_value = {
        "Buckets": [
            {"Name": "a", "CreationDate": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            {"Name": "b"},
        ]
    }
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("lb"))

    result = await fresh_manager.list_buckets("lb")
    assert result == [
        {"name": "a", "creation_date": "2026-01-01T00:00:00+00:00"},
        {"name": "b", "creation_date": None},
    ]


@pytest.mark.asyncio
async def test_list_objects_with_continuation(fresh_manager):
    fake = MagicMock()
    fake.list_objects_v2.return_value = {
        "CommonPrefixes": [{"Prefix": "logs/"}],
        "Contents": [
            {
                "Key": "x",
                "Size": 5,
                "LastModified": datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
                "StorageClass": "STANDARD",
            },
            {"Key": "y", "Size": 0},
        ],
        "IsTruncated": True,
        "NextContinuationToken": "next",
        "KeyCount": 2,
    }
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("lo"))

    res = await fresh_manager.list_objects("lo", "b", "p/", "/", 50, "tok")
    assert res["folders"] == [{"prefix": "logs/"}]
    assert res["objects"][0]["last_modified"] == "2026-04-30T12:00:00+00:00"
    assert res["objects"][1]["last_modified"] is None
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] == "next"
    assert res["key_count"] == 2
    assert fake.list_objects_v2.call_args.kwargs["ContinuationToken"] == "tok"
    assert fake.list_objects_v2.call_args.kwargs["MaxKeys"] == 50


@pytest.mark.asyncio
async def test_list_objects_no_continuation(fresh_manager):
    fake = MagicMock()
    fake.list_objects_v2.return_value = {
        "CommonPrefixes": [],
        "Contents": [],
        "IsTruncated": False,
        "KeyCount": 0,
    }
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("lo2"))

    res = await fresh_manager.list_objects("lo2", "b")
    assert res["folders"] == []
    assert res["objects"] == []
    assert res["is_truncated"] is False
    assert res["next_continuation_token"] is None
    assert "ContinuationToken" not in fake.list_objects_v2.call_args.kwargs


@pytest.mark.asyncio
async def test_get_object_metadata_normalized(fresh_manager):
    fake = MagicMock()
    fake.head_object.return_value = {
        "ContentLength": 42,
        "ContentType": "text/plain",
        "LastModified": datetime(2026, 4, 30, tzinfo=timezone.utc),
        "ETag": '"abc"',
        "StorageClass": "STANDARD",
        "Metadata": {"foo": "bar"},
    }
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("meta"))
    md = await fresh_manager.get_object_metadata("meta", "b", "k")
    assert md["size"] == 42
    assert md["content_type"] == "text/plain"
    assert md["last_modified"].startswith("2026-04-30")
    assert md["metadata"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_get_object_metadata_no_lastmodified(fresh_manager):
    fake = MagicMock()
    fake.head_object.return_value = {"ContentLength": 1, "ContentType": "x"}
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("meta2"))
    md = await fresh_manager.get_object_metadata("meta2", "b", "k")
    assert md["last_modified"] is None
    assert md["metadata"] == {}


@pytest.mark.asyncio
async def test_get_object_passthrough(fresh_manager):
    fake = MagicMock()
    fake.get_object.return_value = {"Body": "stream", "ContentLength": 4}
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("go"))
    res = await fresh_manager.get_object("go", "b", "k")
    assert res == {"Body": "stream", "ContentLength": 4}
    fake.get_object.assert_called_once_with(Bucket="b", Key="k")


@pytest.mark.asyncio
async def test_generate_presigned_url(fresh_manager):
    fake = MagicMock()
    fake.generate_presigned_url.return_value = "https://signed/url"
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(_make_conn("pre"))
    url = await fresh_manager.generate_presigned_url("pre", "b", "k", 1234)
    assert url == "https://signed/url"
    call = fake.generate_presigned_url.call_args
    assert call.args == ("get_object",)
    assert call.kwargs["Params"] == {"Bucket": "b", "Key": "k"}
    assert call.kwargs["ExpiresIn"] == 1234


@pytest.mark.asyncio
async def test_dispose_all(fresh_manager):
    a, b = MagicMock(), MagicMock()
    with patch("app.services.s3_manager.boto3.client", side_effect=[a, b]):
        await fresh_manager.add_connection(_make_conn("a"))
        await fresh_manager.add_connection(_make_conn("b"))
    await fresh_manager.dispose_all()
    assert fresh_manager.list_aliases() == []
    a.close.assert_called_once()
    b.close.assert_called_once()


@pytest.mark.asyncio
async def test_initialize_skips_failures(fresh_manager):
    good = _make_conn("good")
    bad = _make_conn("bad")
    fake = MagicMock()
    call_count = {"n": 0}

    def boto_factory(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return fake
        raise RuntimeError("boom")

    with patch("app.services.s3_manager.boto3.client", side_effect=boto_factory):
        await fresh_manager.initialize([good, bad])

    assert fresh_manager.has_connection("good")
    assert not fresh_manager.has_connection("bad")
