#  Orchestration Engine - File Tool Tests
#
#  Unit tests for sandboxed file path resolution, read, and write tools.
#
#  Depends on: backend/tools/file.py
#  Used by:    pytest

import pytest
from unittest.mock import patch


# We patch DATA_DIR and OUTPUT_BASE so tests don't touch real filesystem
@pytest.fixture
def sandbox(tmp_path):
    """Provide a temporary sandbox directory for file tool tests."""
    output_base = tmp_path / "projects"
    output_base.mkdir()
    with patch("backend.tools.file.OUTPUT_BASE", output_base):
        yield output_base


# Import _safe_path after patching would be too late for module-level,
# so import normally and rely on the patch in each test.
from backend.tools.file import _safe_path, ReadFileTool, WriteFileTool  # noqa: E402


class TestSafePath:
    def test_normal_path(self, sandbox):
        """Normal relative path should resolve inside the project sandbox."""
        result = _safe_path("proj1", "output.txt")
        assert result == (sandbox / "proj1" / "output.txt").resolve()

    def test_nested_path(self, sandbox):
        """Nested relative path should work fine."""
        result = _safe_path("proj1", "subdir/nested/file.md")
        expected = (sandbox / "proj1" / "subdir" / "nested" / "file.md").resolve()
        assert result == expected

    def test_traversal_raises(self, sandbox):
        """Path traversal with ../ escaping the sandbox should raise ValueError."""
        with pytest.raises(ValueError, match="Path traversal"):
            _safe_path("proj1", "../../etc/passwd")

    def test_absolute_path_raises(self, sandbox):
        """Absolute paths should be treated as traversal since they escape the sandbox."""
        with pytest.raises(ValueError, match="Path traversal"):
            _safe_path("proj1", "/etc/passwd")

    def test_dotdot_within_sandbox_ok(self, sandbox):
        """../ that stays within the sandbox should be allowed."""
        # "subdir/../file.txt" resolves to "proj1/file.txt" — still in sandbox
        result = _safe_path("proj1", "subdir/../file.txt")
        expected = (sandbox / "proj1" / "file.txt").resolve()
        assert result == expected

    def test_creates_project_directory_on_write(self, sandbox):
        """_safe_path with ensure_base=True should auto-create the project directory."""
        project_dir = sandbox / "new_project"
        assert not project_dir.exists()
        _safe_path("new_project", "test.txt", ensure_base=True)
        assert project_dir.exists()

    def test_does_not_create_directory_on_read(self, sandbox):
        """_safe_path without ensure_base should NOT create the project directory."""
        project_dir = sandbox / "new_project2"
        assert not project_dir.exists()
        # _safe_path still resolves the path (but the directory doesn't exist yet)
        _safe_path("new_project2", "test.txt")
        assert not project_dir.exists()


class TestReadFileTool:
    async def test_read_existing_file(self, sandbox):
        (sandbox / "proj1").mkdir()
        (sandbox / "proj1" / "hello.txt").write_text("world", encoding="utf-8")

        tool = ReadFileTool()
        result = await tool.execute({"project_id": "proj1", "path": "hello.txt"})
        assert result == "world"

    async def test_read_nonexistent_file(self, sandbox):
        tool = ReadFileTool()
        result = await tool.execute({"project_id": "proj1", "path": "nope.txt"})
        assert "not found" in result.lower()

    async def test_read_traversal_returns_error(self, sandbox):
        tool = ReadFileTool()
        result = await tool.execute({"project_id": "proj1", "path": "../../secret"})
        assert "error" in result.lower()

    async def test_read_truncates_large_file(self, sandbox):
        (sandbox / "proj1").mkdir()
        (sandbox / "proj1" / "big.txt").write_text("x" * 60_000, encoding="utf-8")

        tool = ReadFileTool()
        result = await tool.execute({"project_id": "proj1", "path": "big.txt"})
        assert "truncated" in result
        assert len(result) < 60_000


class TestWriteFileTool:
    async def test_write_creates_file(self, sandbox):
        tool = WriteFileTool()
        result = await tool.execute({
            "project_id": "proj1", "path": "out.txt", "content": "hello"
        })
        assert "written" in result.lower()
        assert (sandbox / "proj1" / "out.txt").read_text(encoding="utf-8") == "hello"

    async def test_write_creates_subdirectories(self, sandbox):
        tool = WriteFileTool()
        result = await tool.execute({
            "project_id": "proj1", "path": "sub/dir/out.txt", "content": "nested"
        })
        assert "written" in result.lower()
        assert (sandbox / "proj1" / "sub" / "dir" / "out.txt").exists()

    async def test_write_traversal_returns_error(self, sandbox):
        tool = WriteFileTool()
        result = await tool.execute({
            "project_id": "proj1", "path": "../../evil.txt", "content": "bad"
        })
        assert "error" in result.lower()
