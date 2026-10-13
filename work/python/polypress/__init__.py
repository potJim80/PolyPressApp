"""Polypress: lossless compression for data tables.

The compressor is the C program in work/csrc; this package only installs it.
Run it as `polypress` (or `python -m polypress`); `polypress --help` lists
the commands. `program()` gives its path, for calling it with subprocess.
"""

import os


def program():
    """Path of the polypress program that came with this package."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "polypress")
