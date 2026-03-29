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
from logging.handlers import RotatingFileHandler
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
    parser.add_argument("--db", type=str, help="SQLite database path (ignored if --dsn or ORCHESTRATION_DSN set)")
    parser.add_argument("--dsn", type=str, help="Postgres DSN (e.g. postgresql://postgres:postgres@localhost:5433/code_storage)")
    parser.add_argument("--max-concurrent", type=int, default=4, help="Max parallel CLI tasks")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--frontend", type=str, help="Path to frontend dist/ directory")
    args = parser.parse_args()

    # Logging — console + rotating file (10 MB × 3 backups)
    level = logging.DEBUG if args.debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline.log")
    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    if args.debug:
        logging.getLogger("gods").setLevel(logging.DEBUG)

    # DB — Postgres DSN takes priority over SQLite path
    dsn = args.dsn or os.environ.get("ORCHESTRATION_DSN")
    db_path = None
    if not dsn:
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
        dsn=dsn,
        max_concurrent=args.max_concurrent,
        frontend_dist=frontend,
    )

    db_label = dsn.split("@")[-1] if dsn and "@" in dsn else (dsn or db_path)
    logging.getLogger("gods.api").info(
        "Starting Hekate on %s:%d (db=%s, max_concurrent=%d, frontend=%s)",
        args.host, args.port, db_label, args.max_concurrent,
        "yes" if frontend else "no",
    )

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
