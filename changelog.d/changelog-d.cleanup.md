Changelog gate hardened: fragments must be named exactly
``changelog.d/<topic>.<type>.md`` (topic ``[a-z0-9][a-z0-9._-]*``, no
subdirectories; allowed types are read fail-closed from
``[[tool.towncrier.type]]`` in ``pyproject.toml``, duplicate types
rejected); every added ``changelog.d/`` file must be a plain regular file
(mode ``100644`` — symlinks, executables, submodules refused) and the added
fragment must have non-whitespace content, read from the committed blob at
``HEAD`` rather than the worktree; ``CHANGELOG.md`` is untouchable in PRs —
any diff on it, including a mode-only change, fails, as does any non-Added
status on a fragment or on the scaffolding.
