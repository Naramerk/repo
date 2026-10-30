Changelog gate hardened: fragments must be named exactly
``changelog.d/<topic>.<type>.md`` (topic ``[a-z0-9][a-z0-9._-]*``, no
subdirectories; allowed types are read fail-closed from
``[[tool.towncrier.type]]`` in ``pyproject.toml``, duplicate types
rejected); ``CHANGELOG.md`` is untouchable in PRs — any diff on it,
including a mode-only change, fails, as does any non-Added status on a
fragment or on the ``changelog.d/README.md``/``.gitkeep`` scaffolding.
