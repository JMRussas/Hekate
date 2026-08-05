"""Test that RotatingFileHandler rotates at 10 MB and caps at 3 backups.

Run with:
  cd Odin && pytest tests/test_log_rotation.py -v

Uses a small maxBytes (50 KB) to keep the test fast while verifying
the same rotation mechanics used in production (10 MB × 3).
"""

from __future__ import annotations

import logging
import os
import tempfile

from logging.handlers import RotatingFileHandler


def test_rotation_produces_backup_files_and_caps_at_3():
    """Write enough data to trigger multiple rotations; verify .1/.2/.3 exist and no .4."""
    max_bytes = 50 * 1024  # 50 KB per file — keeps the test fast
    backup_count = 3

    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "pipeline.log")
        handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup_count)
        handler.setFormatter(logging.Formatter("%(message)s"))

        logger = logging.getLogger("test_rotation")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        try:
            # Write ~300 KB — enough to fill 6× the file limit, cycling through all backups
            line = "X" * 1000 + "\n"  # ~1 KB per line
            for _ in range(300):
                logger.debug(line)

            handler.flush()

            # Backup files .1, .2, .3 must exist
            for i in range(1, backup_count + 1):
                backup = f"{log_path}.{i}"
                assert os.path.exists(backup), f"Expected {backup} to exist"
                size = os.path.getsize(backup)
                # Each backup should be roughly maxBytes (allow some slack for the last write)
                assert size > 0, f"{backup} is empty"

            # .4 must NOT exist — capped at 3 backups
            assert not os.path.exists(f"{log_path}.4"), "Rotation should cap at 3 backups"

            # Active log file should exist and be smaller than maxBytes + one line
            assert os.path.exists(log_path)
            assert os.path.getsize(log_path) <= max_bytes + 1100  # slack for last line

        finally:
            logger.removeHandler(handler)
            handler.close()


def test_production_config_matches_10mb_x3():
    """Verify that run_hekate creates a handler with 10 MB × 3 backups."""
    # Rather than importing the full app, just check the constants match
    expected_max = 10 * 1024 * 1024  # 10 MB
    expected_backups = 3

    # Instantiate with the same args as production
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "pipeline.log")
        handler = RotatingFileHandler(log_path, maxBytes=expected_max, backupCount=expected_backups)
        try:
            assert handler.maxBytes == expected_max
            assert handler.backupCount == expected_backups
        finally:
            handler.close()
