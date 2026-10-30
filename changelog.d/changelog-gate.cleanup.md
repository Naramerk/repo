Changelog-gate follow-ups: the CI job runs with read-only `contents`
permission; a fragment carrying no visible content beyond whitespace and
zero-width Unicode format characters is refused as empty; a missing git
executable or a non-UTF-8 fragment blob is a gate error naming the cause
instead of a traceback; the towncrier floor and fragment-name claims are
documented precisely (an all-digit topic loses its leading zeros at render
time).
