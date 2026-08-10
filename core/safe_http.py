"""Fail-closed HTTP helper for agent egress tools."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests


class UnsafeUrl(ValueError):
    pass


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrl("URL must be http(s), have a hostname, and contain no user-info")
    try:
        records = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrl(f"hostname cannot be resolved: {exc}") from exc
    addresses = {record[4][0] for record in records}
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
    session = requests.Session()
    for _ in range(max_redirects + 1):
        validate_url(current)
        response = session.request(method, current, allow_redirects=False, **kwargs)
        if not response.is_redirect and response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        if _ == max_redirects:
            raise UnsafeUrl("too many redirects")
        current = urljoin(current, location)
        if response.status_code == 303 and method.upper() != "HEAD":
            method = "GET"
            kwargs.pop("json", None)
            kwargs.pop("data", None)
    raise UnsafeUrl("too many redirects")
