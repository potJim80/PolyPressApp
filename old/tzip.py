#!/usr/bin/env python3
"""Polypress -- command line entry point.

The implementation lives in polypress/cli.py so that it can be installed as a
console script. This file stays so the documented `python3 tzip.py ...` form
keeps working from a plain checkout, with no install step.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from polypress.cli import main

if __name__ == "__main__":
    sys.exit(main())
