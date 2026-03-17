#  Migration Validation Tests
#
#  Tests for validate_migration_files() in task_lifecycle.py.
#  Covers: correct naming, invalid ID pattern, wrong down_revision,
#  missing revision variable, and sentinel observation creation.

import os
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_migration(versions_dir, filename, revision, down_revision=None):
    """Write a minimal Alembic migration file."""
    down_line = f"down_revision: str = '{down_revision}'" if down_revision else "down_revision = None"
    content = f"""\
\"\"\"auto-generated migration\"\"\"

revision: str = '{revision}'
{down_line}

from alembic import op
import sqlalchemy as sa


def upgrade():
    pass


def downgrade():
    pass
"""
    fpath = os.path.join(versions_dir, filename)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(content)
    # Touch mtime to be recent (within the 10-minute window)
    now = time.time()
    os.utime(fpath, (now, now))
    return fpath


def _make_versions_dir(tmp_path):
    """Create the backend/migrations/versions/ directory tree."""
    versions_dir = tmp_path / "backend" / "migrations" / "versions"
    versions_dir.mkdir(parents=True)
    return str(versions_dir)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_migration_passes(tmp_path):
    """A correctly-named migration with proper revision/down_revision passes."""
    versions_dir = _make_versions_dir(tmp_path)
    _write_migration(versions_dir, "001_initial.py", "001")
    _write_migration(versions_dir, "002_add_users.py", "002", down_revision="001")

    mock_db = AsyncMock()

    with patch("backend.services.cli_common.resolve_cwd", new_callable=AsyncMock, return_value=str(tmp_path)), \
         patch("backend.services.sentinel.context_client.SentinelContextClient") as mock_ctx:
        mock_ctx_instance = AsyncMock()
        mock_ctx.return_value = mock_ctx_instance

        from backend.services.task_lifecycle import validate_migration_files

        errors = await validate_migration_files(
            db=mock_db,
            project_id="proj1",
            task_id="task1",
            output_text="002_add_users.py",
        )

    assert errors == []


@pytest.mark.asyncio
async def test_invalid_revision_id_pattern(tmp_path):
    """A migration with non-numeric revision ID (e.g., '00A') is caught."""
    versions_dir = _make_versions_dir(tmp_path)
    _write_migration(versions_dir, "001_initial.py", "001")
    # Write a file that has NNN filename but bad revision inside
    _write_migration(versions_dir, "002_bad_rev.py", "00A", down_revision="001")

    mock_db = AsyncMock()

    with patch("backend.services.cli_common.resolve_cwd", new_callable=AsyncMock, return_value=str(tmp_path)), \
         patch("backend.services.sentinel.context_client.SentinelContextClient") as mock_ctx:
        mock_ctx_instance = AsyncMock()
        mock_ctx.return_value = mock_ctx_instance

        from backend.services.task_lifecycle import validate_migration_files

        errors = await validate_migration_files(
            db=mock_db,
            project_id="proj1",
            task_id="task1",
            output_text="002_bad_rev.py",
        )

    assert len(errors) >= 1
    # Should flag revision mismatch (00A != 002) and non-NNN pattern
    revision_errors = [e for e in errors if "00A" in e]
    assert len(revision_errors) >= 1


@pytest.mark.asyncio
async def test_wrong_down_revision(tmp_path):
    """A migration pointing to wrong down_revision is caught."""
    versions_dir = _make_versions_dir(tmp_path)
    _write_migration(versions_dir, "001_initial.py", "001")
    _write_migration(versions_dir, "002_second.py", "002", down_revision="001")
    # 003 should point to 002, but points to 001
    _write_migration(versions_dir, "003_third.py", "003", down_revision="001")

    mock_db = AsyncMock()

    with patch("backend.services.cli_common.resolve_cwd", new_callable=AsyncMock, return_value=str(tmp_path)), \
         patch("backend.services.sentinel.context_client.SentinelContextClient") as mock_ctx:
        mock_ctx_instance = AsyncMock()
        mock_ctx.return_value = mock_ctx_instance

        from backend.services.task_lifecycle import validate_migration_files

        errors = await validate_migration_files(
            db=mock_db,
            project_id="proj1",
            task_id="task1",
            output_text="003_third.py",
        )

    assert len(errors) == 1
    assert "down_revision '001'" in errors[0]
    assert "expected previous revision '002'" in errors[0]


@pytest.mark.asyncio
async def test_sentinel_observation_created_on_error(tmp_path):
    """Validator creates a SentinelObservation when errors are found."""
    versions_dir = _make_versions_dir(tmp_path)
    _write_migration(versions_dir, "001_initial.py", "001")
    _write_migration(versions_dir, "002_bad.py", "002", down_revision="999")

    mock_db = AsyncMock()
    mock_save = AsyncMock()

    with patch("backend.services.cli_common.resolve_cwd", new_callable=AsyncMock, return_value=str(tmp_path)), \
         patch("backend.services.sentinel.context_client.SentinelContextClient") as mock_ctx:
        mock_ctx_instance = MagicMock()
        mock_ctx_instance.save_observation = mock_save
        mock_ctx.return_value = mock_ctx_instance

        from backend.services.task_lifecycle import validate_migration_files

        errors = await validate_migration_files(
            db=mock_db,
            project_id="proj1",
            task_id="task1",
            output_text="002_bad.py",
        )

    assert len(errors) >= 1
    mock_save.assert_called_once()
    observation = mock_save.call_args[0][0]
    assert observation.category == "migration_validation"
    assert observation.severity.value == "warning"
    assert observation.project_id == "proj1"
    assert observation.task_id == "task1"
    assert "errors" in observation.details


@pytest.mark.asyncio
async def test_no_observation_when_valid(tmp_path):
    """No sentinel observation is created when all migrations are valid."""
    versions_dir = _make_versions_dir(tmp_path)
    _write_migration(versions_dir, "001_initial.py", "001")

    mock_db = AsyncMock()

    with patch("backend.services.cli_common.resolve_cwd", new_callable=AsyncMock, return_value=str(tmp_path)), \
         patch("backend.services.sentinel.context_client.SentinelContextClient") as mock_ctx:
        mock_ctx_instance = AsyncMock()
        mock_ctx.return_value = mock_ctx_instance

        from backend.services.task_lifecycle import validate_migration_files

        errors = await validate_migration_files(
            db=mock_db,
            project_id="proj1",
            task_id="task1",
            output_text="001_initial.py",
        )

    assert errors == []
    mock_ctx_instance.save_observation.assert_not_called()


@pytest.mark.asyncio
async def test_missing_revision_variable(tmp_path):
    """A migration file missing the revision variable is caught."""
    versions_dir = _make_versions_dir(tmp_path)
    # Write a file with no revision variable
    fpath = os.path.join(versions_dir, "001_broken.py")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write("# empty migration\ndown_revision = None\n")
    now = time.time()
    os.utime(fpath, (now, now))

    mock_db = AsyncMock()

    with patch("backend.services.cli_common.resolve_cwd", new_callable=AsyncMock, return_value=str(tmp_path)), \
         patch("backend.services.sentinel.context_client.SentinelContextClient") as mock_ctx:
        mock_ctx_instance = AsyncMock()
        mock_ctx.return_value = mock_ctx_instance

        from backend.services.task_lifecycle import validate_migration_files

        errors = await validate_migration_files(
            db=mock_db,
            project_id="proj1",
            task_id="task1",
            output_text="001_broken.py",
        )

    assert len(errors) == 1
    assert "could not find revision variable" in errors[0]


@pytest.mark.asyncio
async def test_first_migration_with_down_revision_is_error(tmp_path):
    """The first migration (lowest NNN) must have down_revision = None."""
    versions_dir = _make_versions_dir(tmp_path)
    _write_migration(versions_dir, "001_initial.py", "001", down_revision="000")

    mock_db = AsyncMock()

    with patch("backend.services.cli_common.resolve_cwd", new_callable=AsyncMock, return_value=str(tmp_path)), \
         patch("backend.services.sentinel.context_client.SentinelContextClient") as mock_ctx:
        mock_ctx_instance = AsyncMock()
        mock_ctx.return_value = mock_ctx_instance

        from backend.services.task_lifecycle import validate_migration_files

        errors = await validate_migration_files(
            db=mock_db,
            project_id="proj1",
            task_id="task1",
            output_text="001_initial.py",
        )

    assert len(errors) == 1
    assert "first migration should have down_revision = None" in errors[0]


@pytest.mark.asyncio
async def test_no_versions_dir_returns_empty(tmp_path):
    """If the versions directory doesn't exist, returns empty (no crash)."""
    mock_db = AsyncMock()

    with patch("backend.services.cli_common.resolve_cwd", new_callable=AsyncMock, return_value=str(tmp_path)):
        from backend.services.task_lifecycle import validate_migration_files

        errors = await validate_migration_files(
            db=mock_db,
            project_id="proj1",
            task_id="task1",
            output_text="",
        )

    assert errors == []


@pytest.mark.asyncio
async def test_no_cwd_returns_empty():
    """If resolve_cwd returns None, returns empty (no crash)."""
    mock_db = AsyncMock()

    with patch("backend.services.cli_common.resolve_cwd", new_callable=AsyncMock, return_value=None):
        from backend.services.task_lifecycle import validate_migration_files

        errors = await validate_migration_files(
            db=mock_db,
            project_id="proj1",
            task_id="task1",
            output_text="",
        )

    assert errors == []
