#  Orchestration Engine - Entry Point
#
#  Launches the FastAPI server via uvicorn.
#
#  Depends on: backend/app.py, backend/config.py, backend/logging_config.py
#  Used by:    (run directly)

import sys

import uvicorn

from backend.logging_config import setup_logging


def main():
    try:
        from backend.config import cfg
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(
        level=cfg("server.log_level", "INFO"),
        fmt=cfg("server.log_format", "json"),
    )

    uvicorn.run(
        "backend.app:app",
        host=cfg("server.host", "0.0.0.0"),
        port=cfg("server.port", 5200),
        reload=cfg("server.reload", False),
    )


if __name__ == "__main__":
    main()
