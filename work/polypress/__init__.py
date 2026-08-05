"""Polypress -- lossless compression for data tables.

    from polypress import dtz, fast
    table = dtz.read_any("data.csv")
    blob = fast.encode(table)

`fast` is the single-shot codec; `stream` is the bounded-memory block
variant for files larger than RAM. `caccel` is an optional C accelerator
that builds itself on first import and falls back to numpy if it cannot.
"""

# Keep in step with pyproject.toml; the app's About screen reads this one.
__version__ = "0.2.0"
