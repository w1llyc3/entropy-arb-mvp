"""Localhost SNDK Entropy ↔ Lighter probe panel.

    pip install -r requirements.txt -r requirements-web.txt
    python3 -m web

Listens on http://127.0.0.1:8765 . There is no host flag: the socket is
always the loopback interface. Opening the page does not arm live trading.
"""
from __future__ import annotations

import argparse

from web.panel import app

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Localhost panel for the SNDK Entropy ↔ Lighter probe")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"loopback port (default {DEFAULT_PORT})")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    import uvicorn
    uvicorn.run(app, host=HOST, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
