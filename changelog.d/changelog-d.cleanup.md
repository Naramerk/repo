Changelog gate hardened: fragments must be named exactly
``changelog.d/<topic>.<type>.md`` (topic ``[a-z0-9][a-z0-9._-]*``, no
subdirectories; allowed types are read fail-closed from
``[[tool.towncrier.type]]`` in ``pyproject.toml``), and a release diff —
``CHANGELOG.md`` modified plus only fragment deletions — now passes the gate
without adding a fragment.
