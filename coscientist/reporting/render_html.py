"""HTML wrap of study-report Markdown (DESIGN §11 / R16a.1)."""

from __future__ import annotations

import html


def wrap_markdown_html(
    *,
    study_id: str,
    markdown: str,
    report_artifact_id: str | None,
    artifact_href: str | None,
) -> str:
    """Wrap the same MD body; caller supplies ``artifact_href`` (shared with MD)."""
    title = html.escape(study_id, quote=True)
    body_md = html.escape(markdown)
    if report_artifact_id is None:
        anchors = ""
    else:
        if artifact_href is None:
            raise ValueError("artifact_href required when report_artifact_id is set")
        aid = html.escape(report_artifact_id, quote=True)
        href = html.escape(artifact_href, quote=True)
        anchors = f'<p id="artifact-{aid}"><a href="{href}">{aid}</a></p>\n'
    return (
        '<!DOCTYPE html>\n<html lang="ru"><head><meta charset="utf-8"/>\n'
        f"<title>{title}</title></head><body>\n"
        f"{anchors}"
        f"<pre>{body_md}</pre>\n</body></html>\n"
    )
