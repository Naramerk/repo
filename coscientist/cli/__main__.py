"""``python -m coscientist.cli`` / console script ``coscientist``."""

from __future__ import annotations

import sys

from coscientist.cli.run import main

if __name__ == "__main__":
    sys.exit(main())
