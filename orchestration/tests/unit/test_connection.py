# tests/unit/test_connection.py
import pytest
import sqlite3
from pathlib import Path

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio

async def test_db_init(tmp_path: Path):
    """Test that the database can be initialized."""
    from backend.db.connection import Database
    db = Database()
    db_file = tmp_path / "test.db"
    
    assert not db_file.exists()
    
    await db.init(db_file)
    assert db_file.exists()
    
    # Check that PRAGMAs were set
    conn = db.conn
    journal_mode = await conn.execute("PRAGMA journal_mode")
    assert (await journal_mode.fetchone())[0] == "wal"
    
    fk = await conn.execute("PRAGMA foreign_keys")
    assert (await fk.fetchone())[0] == 1
    
    await db.close()
    assert db._conn is None

async def test_uninitialized_access(tmp_path: Path):
    """Test that accessing an uninitialized DB raises an error."""
    from backend.db.connection import Database
    db = Database()
    with pytest.raises(RuntimeError, match="Database not initialized"):
        _ = db.conn

async def test_write_and_read(tmp_db):
    """Test basic write and read operations."""
    await tmp_db.execute_write(
        "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
        ("user1", "alice@example.com", "Alice", 0)
    )
    
    row = await tmp_db.fetchone("SELECT * FROM users WHERE id = ?", ("user1",))
    assert row is not None
    assert row["id"] == "user1"
    assert row["display_name"] == "Alice"
    
    row_none = await tmp_db.fetchone("SELECT * FROM users WHERE id = ?", ("user2",))
    assert row_none is None

async def test_fetchall(tmp_db):
    """Test the fetchall method."""
    await tmp_db.execute_many_write([
        ("INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)", ("user1", "a@a.com", "A", 1)),
        ("INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)", ("user2", "b@b.com", "B", 2)),
    ])
    
    rows = await tmp_db.fetchall("SELECT * FROM users ORDER BY created_at")
    assert len(rows) == 2
    assert rows[0]["display_name"] == "A"
    assert rows[1]["display_name"] == "B"

async def test_transaction_commit(tmp_db):
    """Test that a successful transaction commits."""
    async with tmp_db.transaction():
        await tmp_db.execute_write(
            "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
            ("user1", "a@a.com", "A", 1)
        )
        await tmp_db.execute_write(
            "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
            ("user2", "b@b.com", "B", 2)
        )
        
    rows = await tmp_db.fetchall("SELECT * FROM users")
    assert len(rows) == 2

async def test_transaction_rollback(tmp_db):
    """Test that a failed transaction rolls back."""
    with pytest.raises(sqlite3.IntegrityError):
        async with tmp_db.transaction():
            await tmp_db.execute_write(
                "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
                ("user1", "a@a.com", "A", 1)
            )
            # This will fail due to PRIMARY KEY constraint
            await tmp_db.execute_write(
                "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
                ("user1", "a@a.com", "A", 1)
            )

    # After the failed transaction, the table should be empty
    rows = await tmp_db.fetchall("SELECT * FROM users")
    assert len(rows) == 0

async def test_execute_many_write(tmp_db):
    """Test executing multiple statements atomically."""
    statements = [
        ("INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)", ("user1", "a@a.com", "A", 1)),
        ("INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)", ("user2", "b@b.com", "B", 2)),
    ]
    await tmp_db.execute_many_write(statements)
    
    rows = await tmp_db.fetchall("SELECT * FROM users")
    assert len(rows) == 2

async def test_nested_transaction(tmp_db):
    """Test that nested transaction calls are no-ops."""
    async with tmp_db.transaction():
        await tmp_db.execute_write(
            "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
            ("outer1", "o1@o1.com", "Outer 1", 1)
        )
        
        # Nested block
        async with tmp_db.transaction():
             await tmp_db.execute_write(
                 "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
                 ("inner1", "i1@i1.com", "Inner 1", 2)
             )
             
        await tmp_db.execute_write(
            "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
            ("outer2", "o2@o2.com", "Outer 2", 3)
        )

    rows = await tmp_db.fetchall("SELECT * FROM users")
    assert len(rows) == 3
