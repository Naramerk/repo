"""NIR (НИР) Markdown framing over core study-report MD (DESIGN §11 / R16a.3)."""

from __future__ import annotations


def wrap_markdown_nir(*, title: str, markdown: str) -> str:
    """Frame core ``render_markdown_report`` body with a config title only.

    No section/narrative logic — title comes from render config (not hardcoded).
    """
    return f"# {title}\n\n{markdown}"
