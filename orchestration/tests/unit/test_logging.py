#  Orchestration Engine - Structured Logging Tests
#
#  Tests for JSON formatter and context variable propagation.
#
#  Depends on: backend/logging_config.py
#  Used by:    pytest

import json
import logging


from backend.logging_config import (
    JSONFormatter,
    request_id_var,
    set_request_id,
    set_task_id,
    setup_logging,
    task_id_var,
)


class TestJSONFormatter:
    def test_output_is_valid_json(self):
        """JSON formatter produces valid JSON."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello world", args=(), exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["logger"] == "test"
        assert data["message"] == "hello world"
        assert "timestamp" in data

    def test_request_id_included_when_set(self):
        """request_id appears in JSON when context var is set."""
        formatter = JSONFormatter()
        token = request_id_var.set("req-abc123")
        try:
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="with request", args=(), exc_info=None,
            )
            data = json.loads(formatter.format(record))
            assert data["request_id"] == "req-abc123"
        finally:
            request_id_var.reset(token)

    def test_task_id_included_when_set(self):
        """task_id appears in JSON when context var is set."""
        formatter = JSONFormatter()
        token = task_id_var.set("task-xyz")
        try:
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="with task", args=(), exc_info=None,
            )
            data = json.loads(formatter.format(record))
            assert data["task_id"] == "task-xyz"
        finally:
            task_id_var.reset(token)

    def test_context_vars_absent_when_not_set(self):
        """request_id and task_id are omitted when context vars are None."""
        formatter = JSONFormatter()
        # Ensure clean state
        set_request_id(None)
        set_task_id(None)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="no context", args=(), exc_info=None,
        )
        data = json.loads(formatter.format(record))
        assert "request_id" not in data
        assert "task_id" not in data

    def test_exception_included(self):
        """Exception info appears in JSON output."""
        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="oops", args=(), exc_info=exc_info,
        )
        data = json.loads(formatter.format(record))
        assert "exception" in data
        assert "ValueError" in data["exception"]


class TestSetupLogging:
    def test_json_format(self):
        """setup_logging with json format installs JSONFormatter."""
        logger = logging.getLogger("orchestration")
        # Clear any existing handlers
        logger.handlers.clear()

        setup_logging("INFO", "json")
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, JSONFormatter)
        assert logger.level == logging.INFO

        # Cleanup
        logger.handlers.clear()

    def test_text_format(self):
        """setup_logging with text format installs standard Formatter."""
        logger = logging.getLogger("orchestration")
        logger.handlers.clear()

        setup_logging("DEBUG", "text")
        assert len(logger.handlers) == 1
        assert not isinstance(logger.handlers[0].formatter, JSONFormatter)
        assert logger.level == logging.DEBUG

        # Cleanup
        logger.handlers.clear()

    def test_idempotent(self):
        """Calling setup_logging twice doesn't duplicate handlers."""
        logger = logging.getLogger("orchestration")
        logger.handlers.clear()

        setup_logging("INFO", "json")
        setup_logging("INFO", "json")
        assert len(logger.handlers) == 1

        # Cleanup
        logger.handlers.clear()


class TestRequestIDMiddleware:
    async def test_request_id_header_returned(self, app_client):
        """API responses include X-Request-ID header."""
        resp = await app_client.get("/api/health")
        assert resp.status_code == 200
        rid = resp.headers.get("x-request-id")
        assert rid is not None
        assert len(rid) == 12  # uuid hex[:12]
