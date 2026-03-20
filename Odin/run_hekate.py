"""Hekate Engine — entry point.

Starts the FastAPI app with the gods pipeline.
Replaces orchestration/run.py.

Usage:
    python run_hekate.py                          # Start on port 5200
    python run_hekate.py --port 8000              # Custom port
    python run_hekate.py --debug                  # Debug logging
    python run_hekate.py --db /path/to/db.sqlite  # Custom DB path
"""

import argparse
import logging
import os
import sys

# Add Odin to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn

from gods.api import create_app


def main():
    parser = argparse.ArgumentParser(description="Hekate Engine")
    parser.add_argument("--port", type=int, default=5200, help="Port (default 5200)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host (default 0.0.0.0)")
    parser.add_argument("--db", type=str, help="SQLite database path")
    parser.add_argument("--max-concurrent", type=int, default=2, help="Max parallel CLI tasks")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--frontend", type=str, help="Path to frontend dist/ directory")
    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.debug:
        logging.getLogger("gods").setLevel(logging.DEBUG)

    # DB path
    db_path = args.db or os.environ.get(
        "ORCHESTRATION_DB",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "orchestration", "data", "orchestration.db"),
    )

    # Frontend
    frontend = args.frontend or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "orchestration", "frontend", "dist",
    )
    if not os.path.isdir(frontend):
        frontend = None
        logging.getLogger("gods.api").info("No frontend dist/ found — API only mode")

    # Create app
    app = create_app(
        db_path=db_path,
        max_concurrent=args.max_concurrent,
        frontend_dist=frontend,
    )

    logging.getLogger("gods.api").info(
        "Starting Hekate on %s:%d (db=%s, max_concurrent=%d, frontend=%s)",
        args.host, args.port, db_path, args.max_concurrent,
        "yes" if frontend else "no",
    )

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
