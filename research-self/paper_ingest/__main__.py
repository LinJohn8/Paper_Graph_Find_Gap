"""``python -m paper_ingest`` -- the collector's single entry point.

The command surface lives in :mod:`paper_ingest.cli`; this module only wires
it into the module runner so that ``python -m paper_ingest <command>`` works.
"""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
