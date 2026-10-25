"""Typed refuse errors for production profile / tool-catalog load."""

from __future__ import annotations


class ProfileLoadError(ValueError):
    """Fail-closed load: bad packaged payload, path, or catalog consistency."""


class UnknownProfileError(ProfileLoadError):
    """Profile name is not in the exact production catalog."""


class UnknownToolError(ProfileLoadError):
    """Role tool is not an exact name in the versioned profile tool catalog."""


class CatalogVersionError(ProfileLoadError):
    """Pinned tool-catalog version does not match the packaged catalog."""
