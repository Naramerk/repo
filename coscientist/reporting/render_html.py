"""HTML wrap of study-report Markdown (DESIGN §11 / R16a.1)."""

from __future__ import annotations

import html


def wrap_markdown_html(
    *,
    study_id: str,
    markdown: str,
    artifact: tuple[str, str] | None,
    lang: str,
) -> str:
    """Wrap the same MD body.

    ``artifact`` is ``(report_artifact_id, artifact_href)`` or ``None`` —
    no split id/href that can disagree. ``lang`` comes from render config
    (no hardcoded locale).
    """
    title = html.escape(study_id, quote=True)
    body_md = html.escape(markdown)
    lang_attr = html.escape(lang, quote=True)
    if artifact is None:
        anchors = ""
    else:
        report_artifact_id, artifact_href = artifact
        aid = html.escape(report_artifact_id, quote=True)
        href = html.escape(artifact_href, quote=True)
        anchors = f'<p id="artifact-{aid}"><a href="{href}">{aid}</a></p>\n'
    return (
        f'<!DOCTYPE html>\n<html lang="{lang_attr}"><head><meta charset="utf-8"/>\n'
        f"<title>{title}</title></head><body>\n"
        f"{anchors}"
        f"<pre>{body_md}</pre>\n</body></html>\n"
    )
