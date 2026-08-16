#!/usr/bin/env python3
"""Run the plugin's tests: `python3 run_tests.py [-v]`.

Stdlib unittest only — a plugin that has to be installed before it can be
tested is a plugin nobody tests.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    sys.path.insert(0, os.path.join(ROOT, "src"))
    suite = unittest.defaultTestLoader.discover(
        start_dir=os.path.join(ROOT, "tests"), top_level_dir=os.path.join(ROOT, "tests")
    )
    verbosity = 2 if "-v" in sys.argv else 1
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
