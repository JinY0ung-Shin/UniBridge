"""Tests for webhook URL validation (SSRF protection)."""
from __future__ import annotations

import socket

import pytest

from app.services.webhook_security import validate_webhook_url


# ── Scheme & format ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "ws://x.example"])
def test_rejects_non_http_scheme(url):
    with pytest.raises(ValueError, match="http or https"):
        validate_webhook_url(url)


def test_rejects_userinfo_in_url():
    with pytest.raises(ValueError, match="userinfo"):
        validate_webhook_url("https://user:pw@example.com/webhook")


def test_rejects_url_without_hostname():
    with pytest.raises(ValueError, match="hostname"):
        validate_webhook_url("https:///path")


# ── Hostname blocklist ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hostname",
    [
        "localhost",
        "keycloak",
        "etcd",
        "apisix",
        "litellm",
        "prometheus",
        "unibridge-service",
        "keycloak-db",
        "litellm-db",
        "metadata.google.internal",
    ],
)
def test_blocks_internal_hostnames(hostname):
    with pytest.raises(ValueError, match="internal"):
        validate_webhook_url(f"http://{hostname}/hook")


def test_blocklist_is_case_insensitive():
    with pytest.raises(ValueError, match="internal"):
        validate_webhook_url("https://LocalHost/hook")


def test_blocks_cloud_metadata_ip():
    with pytest.raises(ValueError, match="metadata"):
        validate_webhook_url("http://169.254.169.254/")


# ── IP literal blocklist ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",   # private
        "192.168.1.1",
        "172.16.0.1",
        "0.0.0.0",    # unspecified
        "169.254.10.10",  # link-local
        "224.0.0.1",  # multicast
        "[::1]",      # ipv6 loopback
        "[fe80::1]",  # ipv6 link-local
    ],
)
def test_blocks_internal_ip_literals(host):
    with pytest.raises(ValueError, match="private/internal"):
        validate_webhook_url(f"http://{host}/hook")


# ── DNS resolution fallback ─────────────────────────────────────────────────


def test_returns_url_when_dns_resolution_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    # Use a non-IP, non-blocked name so it goes through DNS resolution path
    out = validate_webhook_url("https://this-domain-is-not-resolvable.example.test/")
    assert out == "https://this-domain-is-not-resolvable.example.test/"


def test_blocks_when_dns_resolves_to_private_ip(monkeypatch):
    def fake_getaddrinfo(host, port, type=None):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.99", port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="private/internal"):
        validate_webhook_url("https://looks-public.example.test/path")


def test_allows_when_dns_resolves_to_public_ip(monkeypatch):
    def fake_getaddrinfo(host, port, type=None):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    out = validate_webhook_url("https://public.example.test/path")
    assert out == "https://public.example.test/path"


def test_handles_empty_sockaddr(monkeypatch):
    def fake_getaddrinfo(host, port, type=None):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ())]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    out = validate_webhook_url("https://example.test/")
    assert out == "https://example.test/"


def test_custom_port_respected(monkeypatch):
    captured = {}

    def fake_getaddrinfo(host, port, type=None):  # noqa: A002
        captured["host"] = host
        captured["port"] = port
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    validate_webhook_url("https://example.test:9000/x")
    assert captured["port"] == 9000


def test_default_port_used_when_none(monkeypatch):
    captured = {}

    def fake_getaddrinfo(host, port, type=None):  # noqa: A002
        captured["port"] = port
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    validate_webhook_url("https://example.test/path")
    assert captured["port"] == 443
