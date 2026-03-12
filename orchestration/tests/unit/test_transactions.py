#  Orchestration Engine - Transaction Tests (Phase 2.1)
#
#  Tests for Database.transaction() context manager, execute_write
#  behavior inside/outside transactions, and execute_many_write rollback.
#
#  Depends on: backend/db/connection.py
#  Used by:    pytest

import pytest


class TestTransactionContextManager:
    async def test_commits_on_success(self, tmp_db):
        async with tmp_db.transaction():
            await tmp_db.conn.execute(
                "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
                "VALUES ('tx1', 'TxProject', 'test', 'draft', 1.0, 1.0)"
            )
        row = await tmp_db.fetchone("SELECT * FROM projects WHERE id = 'tx1'")
        assert row is not None
        assert row["name"] == "TxProject"

    async def test_rolls_back_on_exception(self, tmp_db):
        with pytest.raises(ValueError):
            async with tmp_db.transaction():
                await tmp_db.conn.execute(
                    "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
                    "VALUES ('tx2', 'RollbackProject', 'test', 'draft', 1.0, 1.0)"
                )
                raise ValueError("Deliberate failure")
        row = await tmp_db.fetchone("SELECT * FROM projects WHERE id = 'tx2'")
        assert row is None

    async def test_nesting_is_safe(self, tmp_db):
        """Nested transaction() calls are no-ops — the outer one controls commit."""
        async with tmp_db.transaction():
            await tmp_db.conn.execute(
                "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
                "VALUES ('tx3', 'Outer', 'test', 'draft', 1.0, 1.0)"
            )
            async with tmp_db.transaction():
                await tmp_db.conn.execute(
                    "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
                    "VALUES ('tx4', 'Inner', 'test', 'draft', 1.0, 1.0)"
                )
        # Both should be committed
        assert await tmp_db.fetchone("SELECT * FROM projects WHERE id = 'tx3'") is not None
        assert await tmp_db.fetchone("SELECT * FROM projects WHERE id = 'tx4'") is not None


class TestExecuteWriteInTransaction:
    async def test_no_auto_commit_inside_transaction(self, tmp_db):
        """execute_write inside a transaction does NOT auto-commit."""
        async with tmp_db.transaction():
            await tmp_db.execute_write(
                "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
                "VALUES ('ew1', 'NoAutoCommit', 'test', 'draft', 1.0, 1.0)"
            )
            # Still inside transaction — verify the in_transaction flag
            assert tmp_db._in_transaction is True
        # After exiting, should be committed
        row = await tmp_db.fetchone("SELECT * FROM projects WHERE id = 'ew1'")
        assert row is not None

    async def test_auto_commits_outside_transaction(self, tmp_db):
        """execute_write outside a transaction auto-commits as before."""
        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES ('ew2', 'AutoCommit', 'test', 'draft', 1.0, 1.0)"
        )
        assert tmp_db._in_transaction is False
        row = await tmp_db.fetchone("SELECT * FROM projects WHERE id = 'ew2'")
        assert row is not None


class TestExecuteManyWriteRollback:
    async def test_all_statements_commit_on_success(self, tmp_db):
        await tmp_db.execute_many_write([
            (
                "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
                "VALUES ('em1', 'First', 'test', 'draft', 1.0, 1.0)",
                (),
            ),
            (
                "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
                "VALUES ('em2', 'Second', 'test', 'draft', 1.0, 1.0)",
                (),
            ),
        ])
        assert await tmp_db.fetchone("SELECT * FROM projects WHERE id = 'em1'") is not None
        assert await tmp_db.fetchone("SELECT * FROM projects WHERE id = 'em2'") is not None

    async def test_rolls_back_all_on_failure(self, tmp_db):
        """If any statement fails, all prior statements are rolled back."""
        with pytest.raises(Exception):
            await tmp_db.execute_many_write([
                (
                    "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
                    "VALUES ('em3', 'WillRollBack', 'test', 'draft', 1.0, 1.0)",
                    (),
                ),
                # This will fail — duplicate primary key
                (
                    "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
                    "VALUES ('em3', 'Duplicate', 'test', 'draft', 1.0, 1.0)",
                    (),
                ),
            ])
        # The first insert should have been rolled back
        row = await tmp_db.fetchone("SELECT * FROM projects WHERE id = 'em3'")
        assert row is None
