from __future__ import annotations

import ipaddress
import re
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
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("webhook_url must not contain userinfo (user:pass@)")
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


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)

# Below this length a URL fragment is not a credential, and replacing it
# everywhere would corrupt unrelated words in the surrounding message.
_MIN_REDACTED_LENGTH = 4


def mask_webhook_url(url: str) -> str:
    """Reconstruct from hostname/port only so userinfo, path, query, and fragment never leak to non-writers."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "***"
    if not parsed.scheme or not host:
        return "***"
    if port is not None:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}/***"


def _secret_parts(url: str) -> list[str]:
    """The substrings of ``url`` that must never surface in shared text.

    The path is deliberately not one of them: it is already covered by the
    scheme-anchored pass, and a path segment doubles as an ordinary word often
    enough ("/private") that replacing it everywhere corrupts the message.
    """
    parsed = urlparse(url)
    candidates = (parsed.query, parsed.fragment, parsed.username, parsed.password)
    return [part for part in candidates if part and len(part) >= _MIN_REDACTED_LENGTH]


def redact_webhook_url(text: str, url: str) -> str:
    """Strip webhook credentials out of a message that is logged or persisted.

    Every URL in ``text`` is reduced to scheme://host, then any token-bearing
    fragment of ``url`` that survived is replaced: clients render URLs in forms
    that differ from the stored string (normalised, quoted, split across a
    message), so matching the string itself is not enough.
    """
    redacted = _URL_IN_TEXT.sub(lambda match: mask_webhook_url(match.group(0)), text)
    for part in _secret_parts(url):
        redacted = redacted.replace(part, "***")
    return redacted
