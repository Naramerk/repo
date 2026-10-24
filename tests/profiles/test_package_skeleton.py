"""P2: profiles package exists and stays importable without side effects."""

from __future__ import annotations

import importlib

import coscientist.profiles as profiles


def test_profiles_package_importable() -> None:
    mod = importlib.import_module("coscientist.profiles")
    assert mod is profiles
    assert mod.__doc__ is not None
    assert "DESIGN §8" in mod.__doc__


def test_profiles_public_api_includes_models() -> None:
    """P3: domain models are the first public exports."""
    assert "Profile" in profiles.__all__
    assert profiles.Profile.__name__ == "Profile"
