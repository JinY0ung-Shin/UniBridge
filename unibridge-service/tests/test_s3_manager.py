"""Unit tests for S3ConnectionManager."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import threading
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.config import settings
from app.models import S3Connection
from app.services.connection_manager import encrypt_password
from app.services.s3_manager import (
    S3_MAX_PAGE_KEYS,
    S3ConnectionManager,
    S3ListAllBusyError,
    s3_manager,
)


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


def _make_conn(alias="t", endpoint=None, bucket=None, allowed=None) -> S3Connection:
    return S3Connection(
        alias=alias,
        endpoint_url=endpoint,
        region="us-east-1",
        access_key_id_encrypted=encrypt_password("AKIA-TEST"),
        secret_access_key_encrypted=encrypt_password("SECRET"),
        default_bucket=bucket,
        allowed_buckets=allowed,
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


# ── Full listing (?all=true) ────────────────────────────────────────────────


def _page(keys, token=None, prefixes=()):
    """ListObjectsV2 response holding ``keys``; truncated iff ``token`` is set."""
    resp = {
        "CommonPrefixes": [{"Prefix": p} for p in prefixes],
        "Contents": [{"Key": k, "Size": 1} for k in keys],
        "IsTruncated": token is not None,
        "KeyCount": len(keys) + len(prefixes),
    }
    if token is not None:
        resp["NextContinuationToken"] = token
    return resp


async def _manager_with_pages(manager, alias, pages):
    fake = MagicMock()
    fake.list_objects_v2.side_effect = pages
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await manager.add_connection(_make_conn(alias))
    return fake


@pytest.mark.asyncio
async def test_list_all_objects_follows_every_page(fresh_manager):
    fake = await _manager_with_pages(fresh_manager, "all", [
        _page(["a", "b"], token="t1", prefixes=["logs/"]),
        _page(["c"], token="t2"),
        _page(["d"]),
    ])

    res = await fresh_manager.list_all_objects("all", "b", "p/", "/")

    assert res["folders"] == [{"prefix": "logs/"}]
    assert [o["key"] for o in res["objects"]] == ["a", "b", "c", "d"]
    assert res["is_truncated"] is False
    assert res["next_continuation_token"] is None
    assert res["key_count"] == 5
    calls = [c.kwargs for c in fake.list_objects_v2.call_args_list]
    assert [c.get("ContinuationToken") for c in calls] == [None, "t1", "t2"]
    assert all(c["MaxKeys"] == S3_MAX_PAGE_KEYS for c in calls)
    assert all(c["Prefix"] == "p/" and c["Delimiter"] == "/" for c in calls)
    assert not fresh_manager._full_listing_slots().locked()


@pytest.mark.asyncio
async def test_list_all_objects_resumes_from_given_token(fresh_manager):
    fake = await _manager_with_pages(fresh_manager, "resume", [_page(["z"])])

    await fresh_manager.list_all_objects("resume", "b", continuation_token="tok")

    assert fake.list_objects_v2.call_args.kwargs["ContinuationToken"] == "tok"


@pytest.mark.asyncio
async def test_list_all_objects_caps_entries_with_resume_token(fresh_manager, monkeypatch):
    monkeypatch.setattr(settings, "S3_LIST_ALL_MAX_KEYS", 1500)
    fake = await _manager_with_pages(fresh_manager, "cap", [
        _page([f"k{i}" for i in range(1000)], token="t1"),
        _page([f"k{i}" for i in range(1000, 1500)], token="t2"),
    ])

    res = await fresh_manager.list_all_objects("cap", "b")

    assert res["key_count"] == 1500
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] == "t2"
    # The last page is trimmed so the cap is exact and "t2" resumes right
    # after the final returned entry.
    assert [c.kwargs["MaxKeys"] for c in fake.list_objects_v2.call_args_list] == [1000, 500]


@pytest.mark.asyncio
async def test_list_all_objects_stops_at_time_budget(fresh_manager, monkeypatch):
    monkeypatch.setattr(settings, "S3_LIST_ALL_TIME_BUDGET_SECONDS", 0.0)
    fake = await _manager_with_pages(fresh_manager, "budget", [
        _page(["a"], token="t1"),
        _page(["b"]),
    ])

    res = await fresh_manager.list_all_objects("budget", "b")

    assert [o["key"] for o in res["objects"]] == ["a"]
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] == "t1"
    assert fake.list_objects_v2.call_count == 1


@pytest.mark.asyncio
async def test_list_all_objects_later_page_failure_keeps_partial(fresh_manager):
    await _manager_with_pages(fresh_manager, "partial", [
        _page(["a"], token="t1"),
        ClientError({"Error": {"Code": "SlowDown", "Message": "slow"}}, "ListObjectsV2"),
    ])

    res = await fresh_manager.list_all_objects("partial", "b")

    assert [o["key"] for o in res["objects"]] == ["a"]
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] == "t1"


@pytest.mark.asyncio
async def test_list_all_objects_first_page_failure_raises(fresh_manager):
    await _manager_with_pages(
        fresh_manager,
        "denied",
        ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2"),
    )

    with pytest.raises(ClientError):
        await fresh_manager.list_all_objects("denied", "b")
    assert not fresh_manager._full_listing_slots().locked()


@pytest.mark.asyncio
async def test_list_all_objects_truncated_without_token_stops(fresh_manager):
    tokenless = {"Contents": [{"Key": "a", "Size": 1}], "IsTruncated": True, "KeyCount": 1}
    # The larger retry is ignored too: the backend hands back the same page.
    fake = await _manager_with_pages(fresh_manager, "notoken", [tokenless, tokenless])

    res = await fresh_manager.list_all_objects("notoken", "b")

    assert [o["key"] for o in res["objects"]] == ["a"]
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] is None
    assert fake.list_objects_v2.call_count == 2


def _tokenless_backend(keys, ceiling=None):
    """Backend that truncates without a continuation token but honours any
    MaxKeys up to ``ceiling`` (unbounded when None)."""
    def list_page(**kwargs):
        start = keys.index(kwargs["ContinuationToken"]) + 1 if "ContinuationToken" in kwargs else 0
        limit = kwargs["MaxKeys"] if ceiling is None else min(kwargs["MaxKeys"], ceiling)
        chunk = keys[start:start + limit]
        truncated = start + limit < len(keys)
        return {
            "Contents": [{"Key": k, "Size": 1} for k in chunk],
            "IsTruncated": truncated,
            "KeyCount": len(chunk),
        }
    return list_page


@pytest.mark.asyncio
async def test_list_all_objects_tokenless_backend_gets_one_larger_page(fresh_manager):
    keys = [f"k{i:05d}" for i in range(2500)]
    fake = await _manager_with_pages(fresh_manager, "big", _tokenless_backend(keys))

    res = await fresh_manager.list_all_objects("big", "b")

    assert [o["key"] for o in res["objects"]] == keys
    assert res["is_truncated"] is False
    assert res["next_continuation_token"] is None
    calls = [c.kwargs for c in fake.list_objects_v2.call_args_list]
    assert [c["MaxKeys"] for c in calls] == [S3_MAX_PAGE_KEYS, settings.S3_LIST_ALL_TOKENLESS_MAX_KEYS]
    assert all("ContinuationToken" not in c for c in calls)


@pytest.mark.asyncio
async def test_list_all_objects_tokenless_larger_page_is_bounded(fresh_manager, monkeypatch):
    monkeypatch.setattr(settings, "S3_LIST_ALL_TOKENLESS_MAX_KEYS", 1800)
    keys = [f"k{i:05d}" for i in range(2500)]
    fake = await _manager_with_pages(fresh_manager, "bounded", _tokenless_backend(keys))

    res = await fresh_manager.list_all_objects("bounded", "b")

    assert [o["key"] for o in res["objects"]] == keys[:1800]  # the larger page replaces the first
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] is None
    assert [c.kwargs["MaxKeys"] for c in fake.list_objects_v2.call_args_list] == [1000, 1800]


@pytest.mark.asyncio
async def test_list_all_objects_tokenless_backend_capped_at_1000_stays_unresumable(fresh_manager):
    keys = [f"k{i:05d}" for i in range(2500)]
    fake = await _manager_with_pages(fresh_manager, "capped", _tokenless_backend(keys, ceiling=1000))

    res = await fresh_manager.list_all_objects("capped", "b")

    assert [o["key"] for o in res["objects"]] == keys[:1000]
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] is None
    assert fake.list_objects_v2.call_count == 2


@pytest.mark.asyncio
async def test_list_all_objects_tokenless_mid_walk_retries_from_last_token(fresh_manager, monkeypatch):
    monkeypatch.setattr(settings, "S3_LIST_ALL_TOKENLESS_MAX_KEYS", 5000)
    keys = [f"k{i:05d}" for i in range(3000)]
    tokenless = _tokenless_backend(keys)

    def backend(**kwargs):
        if "ContinuationToken" not in kwargs:  # the first page still hands out a token
            return {
                "Contents": [{"Key": k, "Size": 1} for k in keys[:1000]],
                "IsTruncated": True,
                "NextContinuationToken": keys[999],
                "KeyCount": 1000,
            }
        return tokenless(**kwargs)

    fake = await _manager_with_pages(fresh_manager, "mid", backend)

    res = await fresh_manager.list_all_objects("mid", "b")

    assert [o["key"] for o in res["objects"]] == keys
    assert res["is_truncated"] is False
    calls = [(c.kwargs.get("ContinuationToken"), c.kwargs["MaxKeys"]) for c in fake.list_objects_v2.call_args_list]
    assert calls == [(None, 1000), ("k00999", 1000), ("k00999", 5000)]


@pytest.mark.asyncio
async def test_list_all_objects_tokenless_complete_page_of_same_size_is_kept(fresh_manager):
    keys = [f"k{i:05d}" for i in range(1000)]

    def backend(**kwargs):  # flags any full page as truncated, never hands out a token
        chunk = keys[:kwargs["MaxKeys"]]
        return {
            "Contents": [{"Key": k, "Size": 1} for k in chunk],
            "IsTruncated": len(chunk) == kwargs["MaxKeys"],
            "KeyCount": len(chunk),
        }

    await _manager_with_pages(fresh_manager, "full", backend)

    res = await fresh_manager.list_all_objects("full", "b")

    assert res["key_count"] == 1000
    assert res["is_truncated"] is False


@pytest.mark.asyncio
async def test_list_all_objects_tokenless_retry_failure_keeps_the_page(fresh_manager):
    keys = [f"k{i:05d}" for i in range(2500)]
    tokenless = _tokenless_backend(keys)

    def backend(**kwargs):
        if kwargs["MaxKeys"] > S3_MAX_PAGE_KEYS:
            raise ClientError({"Error": {"Code": "InvalidArgument", "Message": "MaxKeys"}}, "ListObjectsV2")
        return tokenless(**kwargs)

    await _manager_with_pages(fresh_manager, "refused", backend)

    res = await fresh_manager.list_all_objects("refused", "b")  # first page: must not raise

    assert [o["key"] for o in res["objects"]] == keys[:1000]
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] is None


@pytest.mark.asyncio
async def test_list_all_objects_tokenless_retry_skipped_past_deadline(fresh_manager, monkeypatch):
    monkeypatch.setattr(settings, "S3_LIST_ALL_TIME_BUDGET_SECONDS", 0.0)
    keys = [f"k{i:05d}" for i in range(2500)]
    fake = await _manager_with_pages(fresh_manager, "late", _tokenless_backend(keys))

    res = await fresh_manager.list_all_objects("late", "b")

    assert res["key_count"] == 1000
    assert res["is_truncated"] is True
    assert fake.list_objects_v2.call_count == 1


@pytest.mark.asyncio
async def test_list_all_objects_stops_when_token_does_not_advance(fresh_manager):
    fake = await _manager_with_pages(fresh_manager, "stuck", [
        _page(["a"], token="t1"),
        _page(["b"], token="t1"),
        _page(["c"]),
    ])

    res = await fresh_manager.list_all_objects("stuck", "b")

    # The page that echoed its token back is dropped: it may repeat "a".
    assert [o["key"] for o in res["objects"]] == ["a"]
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] is None
    assert fake.list_objects_v2.call_count == 2


@pytest.mark.asyncio
async def test_list_all_objects_resume_with_echoed_token_returns_nothing(fresh_manager):
    fake = await _manager_with_pages(fresh_manager, "echo", [_page(["a"], token="t1")])

    res = await fresh_manager.list_all_objects("echo", "b", continuation_token="t1")

    assert res["key_count"] == 0
    assert res["is_truncated"] is True
    assert res["next_continuation_token"] is None
    assert fake.list_objects_v2.call_count == 1


@pytest.mark.asyncio
async def test_list_all_objects_slots_follow_a_size_change(fresh_manager, monkeypatch):
    monkeypatch.setattr(settings, "S3_LIST_ALL_MAX_CONCURRENT", 1)
    one = fresh_manager._full_listing_slots()
    monkeypatch.setattr(settings, "S3_LIST_ALL_MAX_CONCURRENT", 2)
    two = fresh_manager._full_listing_slots()

    assert two is not one
    assert fresh_manager._full_listing_slots() is two
    await two.acquire()
    assert not two.locked()  # a second slot is still free
    two.release()


def _gated_pages(gate):
    def slow_page(**_kwargs):
        gate.wait(5)
        return _page(["a"])
    return slow_page


@pytest.mark.asyncio
async def test_list_all_objects_queues_for_a_free_slot(fresh_manager, monkeypatch):
    monkeypatch.setattr(settings, "S3_LIST_ALL_MAX_CONCURRENT", 1)
    gate = threading.Event()
    fake = await _manager_with_pages(fresh_manager, "queue", None)
    fake.list_objects_v2.side_effect = _gated_pages(gate)

    first = asyncio.create_task(fresh_manager.list_all_objects("queue", "b"))
    second = asyncio.create_task(fresh_manager.list_all_objects("queue", "b"))
    try:
        await asyncio.sleep(0.05)
        assert not second.done()  # waiting for the only slot, not rejected
        assert fake.list_objects_v2.call_count == 1
    finally:
        gate.set()
    assert (await first)["key_count"] == 1
    assert (await second)["key_count"] == 1
    assert not fresh_manager._full_listing_slots().locked()


@pytest.mark.asyncio
async def test_list_all_objects_busy_when_no_slot_frees_within_budget(fresh_manager, monkeypatch):
    monkeypatch.setattr(settings, "S3_LIST_ALL_MAX_CONCURRENT", 1)
    monkeypatch.setattr(settings, "S3_LIST_ALL_TIME_BUDGET_SECONDS", 0.05)
    gate = threading.Event()
    fake = await _manager_with_pages(fresh_manager, "busy", None)
    fake.list_objects_v2.side_effect = _gated_pages(gate)

    first = asyncio.create_task(fresh_manager.list_all_objects("busy", "b"))
    try:
        await asyncio.sleep(0)  # let the first walk take the only slot
        with pytest.raises(S3ListAllBusyError):
            await fresh_manager.list_all_objects("busy", "b")
    finally:
        gate.set()
    await first
    assert not fresh_manager._full_listing_slots().locked()


@pytest.mark.asyncio
async def test_list_all_objects_cancelled_walk_frees_its_slot(fresh_manager, monkeypatch):
    monkeypatch.setattr(settings, "S3_LIST_ALL_MAX_CONCURRENT", 1)
    gate = threading.Event()
    fake = await _manager_with_pages(fresh_manager, "cancel", None)
    fake.list_objects_v2.side_effect = _gated_pages(gate)

    walk = asyncio.create_task(fresh_manager.list_all_objects("cancel", "b"))
    try:
        await asyncio.sleep(0)
        assert fresh_manager._full_listing_slots().locked()
        walk.cancel()
        with pytest.raises(asyncio.CancelledError):
            await walk
        assert not fresh_manager._full_listing_slots().locked()
    finally:
        gate.set()


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


# ── Bucket allow-list ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_connection_parses_allowed_buckets(fresh_manager):
    with patch("app.services.s3_manager.boto3.client", return_value=MagicMock()):
        await fresh_manager.add_connection(
            _make_conn("allowed", allowed='["bucket-a", "bucket-b"]')
        )
    assert fresh_manager.get_config("allowed")["allowed_buckets"] == ["bucket-a", "bucket-b"]
    assert fresh_manager.allowed_buckets("allowed") == ["bucket-a", "bucket-b"]


@pytest.mark.asyncio
async def test_allowed_buckets_none_when_unrestricted(fresh_manager):
    with patch("app.services.s3_manager.boto3.client", return_value=MagicMock()):
        await fresh_manager.add_connection(_make_conn("unrestricted"))
    assert fresh_manager.allowed_buckets("unrestricted") is None


def test_allowed_buckets_none_for_unknown_alias(fresh_manager):
    assert fresh_manager.allowed_buckets("nope") is None


@pytest.mark.asyncio
async def test_add_connection_invalid_allowed_buckets_json_is_unrestricted(fresh_manager):
    with patch("app.services.s3_manager.boto3.client", return_value=MagicMock()):
        await fresh_manager.add_connection(_make_conn("badjson", allowed="not json"))
    assert fresh_manager.allowed_buckets("badjson") is None


@pytest.mark.asyncio
async def test_add_connection_non_list_allowed_buckets_is_unrestricted(fresh_manager):
    with patch("app.services.s3_manager.boto3.client", return_value=MagicMock()):
        await fresh_manager.add_connection(_make_conn("notalist", allowed='{"bucket": true}'))
    assert fresh_manager.allowed_buckets("notalist") is None


@pytest.mark.asyncio
async def test_add_connection_empty_allowed_buckets_list_is_unrestricted(fresh_manager):
    with patch("app.services.s3_manager.boto3.client", return_value=MagicMock()):
        await fresh_manager.add_connection(_make_conn("emptylist", allowed="[]"))
    assert fresh_manager.allowed_buckets("emptylist") is None


@pytest.mark.asyncio
async def test_test_connection_heads_first_allowed_bucket(fresh_manager):
    fake = MagicMock()
    fake.head_bucket.return_value = {}
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(
            _make_conn("allowedtest", allowed='["bucket-a", "bucket-b"]')
        )
    ok, _msg = await fresh_manager.test_connection("allowedtest")
    assert ok is True
    fake.head_bucket.assert_called_once_with(Bucket="bucket-a")
    fake.list_buckets.assert_not_called()


@pytest.mark.asyncio
async def test_test_connection_prefers_default_bucket_over_allowlist(fresh_manager):
    fake = MagicMock()
    fake.head_bucket.return_value = {}
    with patch("app.services.s3_manager.boto3.client", return_value=fake):
        await fresh_manager.add_connection(
            _make_conn("bothset", bucket="bucket-b", allowed='["bucket-a", "bucket-b"]')
        )
    ok, _msg = await fresh_manager.test_connection("bothset")
    assert ok is True
    fake.head_bucket.assert_called_once_with(Bucket="bucket-b")
