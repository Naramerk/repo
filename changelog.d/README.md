# Changelog fragments (towncrier)

Every PR adds **exactly one** new fragment file here and never edits
`CHANGELOG.md` (enforced by `scripts/check_changelog_fragment.py`, CI job
`changelog-fragment`).

File name: exactly `changelog.d/<topic>.<type>.md` — no subdirectories, no
other shapes.

- `<topic>` — non-empty, `[a-z0-9][a-z0-9._-]*` (branch or topic name, also
  shown after the rendered bullet). Dots are allowed: towncrier 26.9.0
  (`_builder.parse_newfragment_basename`) attributes the same topic and type
  to every name the gate accepts, so `a.b.fix.md` is fine; the gate is only
  stricter, refusing names such as `a.fix.1.md` (towncrier would read `.1`
  as a counter) or `a.fix.md.bak`.
- `<type>` — one of the `[[tool.towncrier.type]]` `directory` (or `name`)
  values in `pyproject.toml`: `cleanup`, `feat`, `fix`, `refactor`. The gate
  reads the type set from there (via `tomllib`) and fails closed when
  `pyproject.toml` cannot be read or declares no types.

Everything else added under `changelog.d/` — wrong name, unknown type, or a
subdirectory such as `sub/README.md` — is an explicit gate error naming the
path. The top-level `README.md` and `.gitkeep` are add-only scaffolding,
not fragments: adding them is allowed (and does not count as the PR's
fragment), while deleting, modifying, or renaming them is a gate error.

Every added file under `changelog.d/` — fragment or scaffolding — must be a
plain regular file with mode `100644`: symlinks (`120000`), executables
(`100755`), and submodules (`160000`) are refused by exact mode name.

Content: plain markdown text, no leading `-` (towncrier adds the bullet and
appends `(<topic>)`), and non-whitespace: an empty or whitespace-only
fragment is a gate error. Content is judged from the committed blob at
`HEAD` (the git object store), never from the worktree. Example
`changelog.d/sandbox-fix.fix.md`:

    Sandbox probe no longer skips when cgroup delegation is unavailable.

Releasing: `towncrier build --version X.Y.Z` renders the accumulated
fragments into `CHANGELOG.md` and deletes the used fragments — a manual
maintainer operation outside this gate, so a `CHANGELOG.md` diff never
passes a feature PR. Land a release either as a direct commit to the
release branch, or through a PR whose `changelog-fragment` gate is
expectedly red; such a PR merges only at the maintainer's discretion.

Placement note: `start_string` in `pyproject.toml` is the `# Changelog`
title line, so a new release is rendered directly under the title, **above**
the historical `## Unreleased` block. That block is the pre-towncrier
archive; at the first real release the maintainer may rename or remove it by
hand.

Preview a build: `uvx towncrier@26.9.0 build --draft --version X.Y.Z`.
