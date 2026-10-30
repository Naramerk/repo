# Changelog fragments (towncrier)

Every PR adds **exactly one** new fragment file here and never edits
`CHANGELOG.md` (enforced by `scripts/check_changelog_fragment.py`, CI job
`changelog-fragment`).

File name: exactly `changelog.d/<topic>.<type>.md` — no subdirectories, no
other shapes.

- `<topic>` — non-empty, `[a-z0-9][a-z0-9._-]*` (branch or topic name, also
  shown after the rendered bullet).
- `<type>` — one of the `[[tool.towncrier.type]]` `directory` (or `name`)
  values in `pyproject.toml`: `cleanup`, `feat`, `fix`, `refactor`. The gate
  reads the type set from there (via `tomllib`) and fails closed when
  `pyproject.toml` cannot be read or declares no types.

Anything else added under `changelog.d/` — wrong name, unknown type, or a
subdirectory such as `sub/README.md` — is an explicit gate error naming the
path. The top-level `README.md` and `.gitkeep` are scaffolding, not
fragments.

Content: plain markdown text, no leading `-` (towncrier adds the bullet and
appends `(<topic>)`). Example `changelog.d/sandbox-fix.fix.md`:

    Sandbox probe no longer skips when cgroup delegation is unavailable.

Release path: `towncrier build --version X.Y.Z` is the only change allowed to
touch `CHANGELOG.md`. The gate passes such a diff when `CHANGELOG.md` is
modified, at least one validly named fragment is deleted, nothing is added
under `changelog.d/`, and no fragment carries any other status — exactly
what `towncrier build` produces (it renders the notes and deletes the used
fragments). Every other `CHANGELOG.md` edit still fails the gate.

Placement note: `start_string` in `pyproject.toml` is the `# Changelog`
title line, so a new release is rendered directly under the title, **above**
the historical `## Unreleased` block. That block is the pre-towncrier
archive; at the first real release the maintainer may rename or remove it by
hand.

Preview a build: `uvx towncrier@24.8.0 build --draft --version X.Y.Z`.
