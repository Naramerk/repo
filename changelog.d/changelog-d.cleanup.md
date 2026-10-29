Changelog moved to towncrier fragments: every PR adds exactly one
``changelog.d/<topic>.<type>.md`` (types: cleanup, feat, fix, refactor)
and never edits ``CHANGELOG.md`` — enforced fail-closed by
``scripts/check_changelog_fragment.py`` and the CI ``changelog-fragment``
job (``git diff --name-status <base>...HEAD``).
