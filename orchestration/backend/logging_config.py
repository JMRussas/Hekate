#  Orchestration Engine - Logging Configuration
#
#  Configures structured logging with JSON or text format.
#  Provides context variables for request_id and task_id propagation.
#
#  Depends on: (none)
#  Used by:    run.py, app.py, services/executor.py

import contextvars
import json
import logging
import sys
import time

# Context variables for request/task tracing
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("task_id", default=None)


def set_request_id(rid: str | None):
    request_id_var.set(rid)


def set_task_id(tid: str | None):
    task_id_var.set(tid)


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON with context variables."""

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Inject context vars when present
        rid = request_id_var.get(None)
        if rid:
            entry["request_id"] = rid
        tid = task_id_var.get(None)
        if tid:
            entry["task_id"] = tid
        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


_TEXT_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"


def setup_logging(level: str = "INFO", fmt: str = "json"):
    """Configure structured logging for the orchestration engine.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        fmt: Log format — "json" for structured output, "text" for human-readable.
    """
    root = logging.getLogger("orchestration")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        if fmt == "json":
            handler.setFormatter(JSONFormatter())
        else:
            handler.setFormatter(logging.Formatter(_TEXT_FORMAT, datefmt="%H:%M:%S"))
        root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
