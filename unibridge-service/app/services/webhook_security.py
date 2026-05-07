from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


_BLOCKED_HOSTNAMES = frozenset({
    "localhost", "keycloak", "etcd", "apisix",
    "litellm", "prometheus", "unibridge-service",
    "keycloak-db", "litellm-db",
    "metadata.google.internal",
})


def _is_internal_ip(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_webhook_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("webhook_url must use http or https scheme")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("webhook_url must include a hostname")

    hostname_lower = hostname.lower()
    if hostname_lower in _BLOCKED_HOSTNAMES:
        raise ValueError("webhook_url cannot target internal services")
    if hostname_lower == "169.254.169.254":
        raise ValueError("webhook_url cannot target cloud metadata endpoint")

    try:
        if _is_internal_ip(hostname):
            raise ValueError("webhook_url cannot target private/internal addresses")
        return url
    except ValueError as exc:
        if "private/internal" in str(exc):
            raise

    try:
        addr_infos = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return url

    for addr_info in addr_infos:
        sockaddr = addr_info[4]
        if not sockaddr:
            continue
        if _is_internal_ip(str(sockaddr[0])):
            raise ValueError("webhook_url cannot target private/internal addresses")

    return url
