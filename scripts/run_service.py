"""systemd entry point (deploy/systemd/agentic-portfolio.service). Runs the firm."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.argv[0] = str(Path(__file__).with_name("firm_service.py"))
    runpy.run_path(sys.argv[0], run_name="__main__")
