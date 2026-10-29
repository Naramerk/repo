# Changelog fragments (towncrier)

Every PR adds **exactly one** new fragment file here and never edits
`CHANGELOG.md` (enforced by `scripts/check_changelog_fragment.py`, CI job
`changelog-fragment`).

File name: `<topic>.<type>.md`, e.g. `changelog-d.cleanup.md`.

- `<topic>` — branch or topic name (also shown after the rendered bullet).
- `<type>` — one of `cleanup`, `feat`, `fix`, `refactor` (the CHANGELOG.md
  section kinds).

Content: plain markdown text, no leading `-` (towncrier adds the bullet and
appends `(<topic>)`). Example `changelog.d/sandbox-fix.fix.md`:

    Sandbox probe no longer skips when cgroup delegation is unavailable.

At release time `towncrier build --version X.Y.Z` renders the fragments into
`CHANGELOG.md` above `## Unreleased` and the used fragments are deleted.
Preview a build: `uvx towncrier@24.8.0 build --draft --version X.Y.Z`.
