"""Fail-closed HTTP helper for agent egress tools."""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

import requests


class UnsafeUrl(ValueError):
    pass


_thread_local = threading.local()
_DNS_CACHE_TTL = 5.0


def _session() -> requests.Session:
    """Reuse one HTTP session per calling thread without sharing mutable state."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def validate_url(url: str) -> None:
    # Resolve synchronously immediately before each hop. Keeping this check
    # fail-closed prevents private/loopback SSRF and DNS-rebinding bypasses.
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrl("URL must be http(s), have a hostname, and contain no user-info")
    cache = getattr(_thread_local, "dns_cache", None)
    if cache is None:
        cache = {}
        _thread_local.dns_cache = cache
    cache_key = (parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80))
    cached = cache.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _DNS_CACHE_TTL:
        addresses = set(cached[1])
    else:
        try:
            records = socket.getaddrinfo(cache_key[0], cache_key[1], type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeUrl(f"hostname cannot be resolved: {exc}") from exc
        addresses = {record[4][0] for record in records}
        cache[cache_key] = (time.monotonic(), tuple(addresses))
    if not addresses:
        raise UnsafeUrl("hostname has no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise UnsafeUrl(f"destination resolves to a blocked address: {address}")


def request(method: str, url: str, *, max_redirects: int = 5, **kwargs) -> requests.Response:
    """Request with DNS validation before every hop and no implicit redirects."""
    current = url
    session = _session()
    for _ in range(max_redirects + 1):
        validate_url(current)
        response = session.request(method, current, allow_redirects=False, **kwargs)
        if not response.is_redirect and response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        if _ == max_redirects:
            response.close()
            raise UnsafeUrl("too many redirects")
        current = urljoin(current, location)
        response.close()
        if response.status_code == 303 and method.upper() != "HEAD":
            method = "GET"
            kwargs.pop("json", None)
            kwargs.pop("data", None)
    raise UnsafeUrl("too many redirects")
