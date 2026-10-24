"""Extract fetchable URIs from typed MCP content fields only (no text scraping)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from coscientist.mcp.errors import McpProtocolError, McpUnsupportedUriError


def uris_from_call_result(
    result: dict[str, Any],
    *,
    allowed_schemes: frozenset[str],
) -> tuple[str, ...]:
    """Return URIs from ``content[]`` resource / resource_link entries.

    Raises :class:`McpUnsupportedUriError` when a typed URI field uses a scheme
    outside ``allowed_schemes``. Raises :class:`McpProtocolError` when required
    typed fields are missing or wrong type.
    """
    content = result.get("content")
    if content is None:
        return ()
    if not isinstance(content, list):
        raise McpProtocolError("call result content must be a list")
    found: list[str] = []
    for index, item in enumerate(content):
        if not isinstance(item, dict):
            raise McpProtocolError(f"content[{index}] must be an object")
        item_type = item.get("type")
        if not isinstance(item_type, str):
            raise McpProtocolError(f"content[{index}].type must be a string")
        if item_type == "resource_link":
            found.append(_require_fetchable_uri(item, path=f"content[{index}]", allowed_schemes=allowed_schemes))
        elif item_type == "resource":
            resource = item.get("resource")
            if not isinstance(resource, dict):
                raise McpProtocolError(f"content[{index}].resource must be an object")
            # Inline payload already present — no fetch (typed shape, not a guess).
            if "text" in resource or "blob" in resource:
                continue
            found.append(
                _require_fetchable_uri(
                    resource,
                    path=f"content[{index}].resource",
                    allowed_schemes=allowed_schemes,
                )
            )
    return tuple(found)


def _require_fetchable_uri(
    obj: dict[str, Any],
    *,
    path: str,
    allowed_schemes: frozenset[str],
) -> str:
    uri = obj.get("uri")
    if not isinstance(uri, str) or uri == "":
        raise McpProtocolError(f"{path}.uri must be a non-empty string")
    scheme = urlparse(uri).scheme.lower()
    if scheme not in allowed_schemes:
        raise McpUnsupportedUriError(
            f"{path}.uri scheme {scheme!r} is not in fetch_uri_schemes",
            uri=uri,
        )
    return uri
