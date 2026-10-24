"""Extract fetchable URIs from typed MCP content fields only (no text scraping)."""

from __future__ import annotations

from typing import Any

from coscientist.mcp.errors import McpProtocolError
from coscientist.mcp.ssrf import assert_fetch_scheme


def uris_from_call_result(
    result: dict[str, Any],
    *,
    allowed_schemes: frozenset[str],
) -> tuple[str, ...]:
    """Return URIs from ``content[]`` resource / resource_link entries.

    Schemes outside http/https are hard-denied in code; profile schemes may only
    narrow further. Raises :class:`McpProtocolError` when typed fields are wrong.
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
            found.append(_require_uri(item, path=f"content[{index}]", allowed_schemes=allowed_schemes))
        elif item_type == "resource":
            resource = item.get("resource")
            if not isinstance(resource, dict):
                raise McpProtocolError(f"content[{index}].resource must be an object")
            if "text" not in resource and "blob" not in resource:
                raise McpProtocolError(
                    f"content[{index}].resource embedded resource requires text or blob"
                )
            continue  # embedded payload — never fetch uri
    return tuple(found)


def _require_uri(
    obj: dict[str, Any],
    *,
    path: str,
    allowed_schemes: frozenset[str],
) -> str:
    uri = obj.get("uri")
    if not isinstance(uri, str) or uri == "":
        raise McpProtocolError(f"{path}.uri must be a non-empty string")
    assert_fetch_scheme(uri, allowed_schemes=allowed_schemes)
    return uri
