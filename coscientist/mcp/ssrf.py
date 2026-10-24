"""SSRF policy for MCP file URLs: scheme hard-deny, private IP deny, resolve+pin."""

from __future__ import annotations

import ipaddress
import socket
from typing import Protocol
from urllib.parse import urlparse, urlunparse

from coscientist.mcp.errors import McpSsrfError, McpUnsupportedUriError

_HARD_SCHEMES = frozenset({"http", "https"})


class HostnameResolver(Protocol):
    async def resolve(self, hostname: str) -> tuple[str, ...]:
        """Return one or more IP address strings for ``hostname``."""


class AsyncDnsResolver:
    """stdlib ``getaddrinfo`` resolver (no caching — each fetch resolves anew)."""

    async def resolve(self, hostname: str) -> tuple[str, ...]:
        import asyncio

        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                hostname,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise McpSsrfError(f"DNS resolution failed for host {hostname!r}", uri=hostname) from exc
        addrs: list[str] = []
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            addrs.append(str(sockaddr[0]))
        if not addrs:
            raise McpSsrfError(f"DNS returned no addresses for host {hostname!r}", uri=hostname)
        # Preserve order, unique
        seen: set[str] = set()
        out: list[str] = []
        for a in addrs:
            if a not in seen:
                seen.add(a)
                out.append(a)
        return tuple(out)


def assert_fetch_scheme(uri: str, *, allowed_schemes: frozenset[str]) -> None:
    """Hard-deny non-http(s) in code; profile schemes may only narrow further."""
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme not in _HARD_SCHEMES:
        raise McpUnsupportedUriError(
            f"URI scheme {scheme!r} hard-denied (only http/https)",
            uri=uri,
        )
    if scheme not in allowed_schemes:
        raise McpUnsupportedUriError(
            f"URI scheme {scheme!r} is not in fetch_uri_schemes",
            uri=uri,
        )


def parse_host_port(uri: str) -> tuple[str, int | None, str]:
    """Return (hostname, port|None, scheme). Raises McpSsrfError on missing host."""
    parsed = urlparse(uri)
    if not parsed.hostname:
        raise McpSsrfError("URI has no hostname", uri=uri)
    return parsed.hostname, parsed.port, parsed.scheme.lower()


def ip_is_blocked(ip_str: str) -> bool:
    """True for loopback, link-local, private, multicast, unspecified, reserved, ULA."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or _is_metadata_ipv4(ip)
    )


def _is_metadata_ipv4(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Cloud metadata well-known address (also covered by link-local for 169.254/16).
    if isinstance(ip, ipaddress.IPv4Address):
        return ip == ipaddress.IPv4Address("169.254.169.254")
    return False


def host_literal_ip(hostname: str) -> str | None:
    """If hostname is an IP literal (incl. decimal/octal IPv4 forms), return canonical IP."""
    # Bracketed IPv6 from urlparse comes without brackets in .hostname
    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        pass
    # Decimal / octal / hex IPv4 trick forms (e.g. 2130706433, 0177.0.0.1)
    if _looks_like_ipv4_trick(hostname):
        packed = _parse_ipv4_trick(hostname)
        if packed is not None:
            return str(ipaddress.IPv4Address(packed))
    return None


def _looks_like_ipv4_trick(hostname: str) -> bool:
    if not hostname:
        return False
    # Digits and dots only, or single integer, or 0x-prefixed parts
    allowed = set("0123456789.xXaAbBcCdDeEfF")
    return all(ch in allowed for ch in hostname)


def _parse_ipv4_trick(hostname: str) -> int | None:
    """Parse unusual IPv4 literals to a 32-bit int; None if not parseable."""
    if hostname.isdigit():
        value = int(hostname, 10)
        if 0 <= value <= 0xFFFFFFFF:
            return value
        return None
    parts = hostname.split(".")
    if not (1 <= len(parts) <= 4):
        return None
    nums: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part.lower().startswith("0x"):
                nums.append(int(part, 16))
            elif part.startswith("0") and len(part) > 1 and part.isdigit():
                nums.append(int(part, 8))
            else:
                nums.append(int(part, 10))
        except ValueError:
            return None
    if any(n < 0 for n in nums):
        return None
    if len(nums) == 4:
        if any(n > 255 for n in nums):
            return None
        return (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]
    if len(nums) == 1:
        if nums[0] > 0xFFFFFFFF:
            return None
        return nums[0]
    if len(nums) == 2:
        if nums[0] > 255 or nums[1] > 0xFFFFFF:
            return None
        return (nums[0] << 24) | nums[1]
    if len(nums) == 3:
        if nums[0] > 255 or nums[1] > 255 or nums[2] > 0xFFFF:
            return None
        return (nums[0] << 24) | (nums[1] << 16) | nums[2]
    return None


async def resolve_and_pin(
    uri: str,
    *,
    resolver: HostnameResolver,
    allowed_schemes: frozenset[str],
) -> tuple[str, str, str]:
    """Validate URI and return ``(uri, connect_ip, host_header_value)``.

    All resolved addresses must pass the blocklist (fail-closed). The first
    allowed address is used as the connect target (anti DNS rebinding: connect
    to the pinned IP, not a later re-resolution).
    """
    assert_fetch_scheme(uri, allowed_schemes=allowed_schemes)
    host, _port, _scheme = parse_host_port(uri)
    literal = host_literal_ip(host)
    if literal is not None:
        if ip_is_blocked(literal):
            raise McpSsrfError(f"blocked IP literal {literal}", uri=uri)
        return uri, literal, host
    addrs = await resolver.resolve(host)
    for addr in addrs:
        if ip_is_blocked(addr):
            raise McpSsrfError(f"resolved address {addr} is blocked", uri=uri)
    return uri, addrs[0], host


def redirect_url(base_uri: str, location: str) -> str:
    """Resolve a redirect Location against the base URI (absolute or relative)."""
    from urllib.parse import urljoin

    if not location:
        raise McpSsrfError("empty redirect Location", uri=base_uri)
    return urljoin(base_uri, location)


def rewrite_url_for_connect(uri: str, connect_ip: str) -> str:
    """Replace hostname with pinned IP; caller must set Host header separately."""
    parsed = urlparse(uri)
    # IPv6 connect targets need brackets in netloc
    try:
        ip = ipaddress.ip_address(connect_ip)
        host = f"[{connect_ip}]" if isinstance(ip, ipaddress.IPv6Address) else connect_ip
    except ValueError as exc:
        raise McpSsrfError(f"invalid connect IP {connect_ip!r}", uri=uri) from exc
    if parsed.port is not None:
        netloc = f"{host}:{parsed.port}"
    else:
        netloc = host
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
