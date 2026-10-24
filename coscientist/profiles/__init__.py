"""Versioned research profiles (DESIGN §8).

Domain knowledge lives only in packaged profile config. This package exposes
loaders and Protocols; it must not hardcode medicine/microfluidics behaviour
in Python, and ``coscientist.core`` must not import it.
"""

__all__: list[str] = []
