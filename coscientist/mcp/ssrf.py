"""SSRF policy for MCP file URLs: scheme hard-deny, non-global IP deny, resolve+pin."""

from __future__ import annotations

import ipaddress
import socket
from typing import Protocol
from urllib.parse import urlparse, urlunparse

from coscientist.mcp.errors import McpSsrfError, McpUnsupportedUriError
from coscientist.mcp.schemes import HARD_FETCH_SCHEMES


class HostnameResolver(Protocol):
    async def resolve(self, hostname: str) -> tuple[str, ...]:
        """Return one or more IP address strings for ``hostname``."""


class AddressPolicy(Protocol):
    def is_blocked(self, ip: str) -> bool:
        """True when ``ip`` must not be contacted."""


class GlobalAddressPolicy:
    """Block any address that is not globally routable (after unwrapping)."""

    def is_blocked(self, ip: str) -> bool:
        return ip_is_blocked(ip)


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
    if scheme not in HARD_FETCH_SCHEMES:
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


def unwrap_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unwrap IPv4-mapped and 6to4 addresses to the embedded IPv4."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return mapped
        sixtofour = ip.sixtofour
        if sixtofour is not None:
            return sixtofour
    return ip


def ip_is_blocked(ip_str: str) -> bool:
    """True when the address is not globally routable (after unwrap).

    Uses ``not ip.is_global`` (covers RFC1918, loopback, link-local, CGNAT
    100.64/10, unspecified, reserved). Also blocks multicast (CPython reports
    many multicast addresses as ``is_global``) and deprecated IPv6 site-local
    ``fec0::/10``.
    """
    try:
        ip = unwrap_ip(ipaddress.ip_address(ip_str))
    except ValueError:
        return True
    if ip.is_multicast:
        return True
    if not ip.is_global:
        return True
    # CPython may report deprecated site-local fec0::/10 as is_global True.
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        return True
    return False


def host_literal_ip(hostname: str) -> str | None:
    """If hostname is an IP literal (incl. decimal/octal IPv4 forms), return canonical IP."""
    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        pass
    if _looks_like_ipv4_trick(hostname):
        packed = _parse_ipv4_trick(hostname)
        if packed is not None:
            return str(ipaddress.IPv4Address(packed))
    return None


def _looks_like_ipv4_trick(hostname: str) -> bool:
    if not hostname:
        return False
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
    address_policy: AddressPolicy | None = None,
) -> tuple[str, str, str]:
    """Validate URI and return ``(uri, connect_ip, server_hostname)``.

    All resolved addresses must pass the address policy (fail-closed). The first
    address is the TCP connect target (anti DNS rebinding).
    """
    policy: AddressPolicy = address_policy if address_policy is not None else GlobalAddressPolicy()
    assert_fetch_scheme(uri, allowed_schemes=allowed_schemes)
    host, _port, _scheme = parse_host_port(uri)
    literal = host_literal_ip(host)
    if literal is not None:
        if policy.is_blocked(literal):
            raise McpSsrfError(f"blocked IP literal {literal}", uri=uri)
        return uri, literal, host
    addrs = await resolver.resolve(host)
    for addr in addrs:
        if policy.is_blocked(addr):
            raise McpSsrfError(f"resolved address {addr} is blocked", uri=uri)
    return uri, addrs[0], host


def redirect_url(base_uri: str, location: str) -> str:
    """Resolve a redirect Location against the base URI (absolute or relative)."""
    from urllib.parse import urljoin

    if not location:
        raise McpSsrfError("empty redirect Location", uri=base_uri)
    return urljoin(base_uri, location)


def rewrite_url_host(uri: str, connect_ip: str) -> str:
    """Replace URL hostname with ``connect_ip`` for the TCP peer (Host/SNI stay separate)."""
    parsed = urlparse(uri)
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
