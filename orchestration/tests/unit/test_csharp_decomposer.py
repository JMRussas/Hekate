#  Orchestration Engine - C# Decomposer Tests
#
#  Tests for C# method task handling and assembly task auto-creation.
#
#  Depends on: backend/services/decomposer.py
#  Used by:    CI

import json
from unittest.mock import AsyncMock, patch

from backend.services.decomposer import _create_csharp_assembly_tasks


class TestCsharpContextInjection:
    """Test that target_signature, available_methods, constructor_params
    are injected into task context_json during decomposition."""

    async def test_csharp_method_context_injected(self, tmp_db):
        """Verify C# fields appear in context_json after decomposition."""
        from backend.services.decomposer import DecomposerService

        # Set up project + plan with C# method tasks
        now = 1000.0
        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, config_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 'ready', '{}', ?, ?)",
            ("proj1", "Test", "Build stuff", now, now),
        )

        plan_data = {
            "summary": "Implement UserService",
            "phases": [{
                "name": "UserService",
                "tasks": [{
                    "title": "UserService.GetUser",
                    "description": "Fetch user by ID",
                    "task_type": "csharp_method",
                    "complexity": "medium",
                    "depends_on": [],
                    "target_class": "MyApp.Services.UserService",
                    "target_signature": "public async Task<User> GetUser(Guid id)",
                    "available_methods": ["public void Save(User u)"],
                    "constructor_params": ["IDbContext db", "ILogger logger"],
                    "requirement_ids": ["R1"],
                }],
            }],
        }

        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
            ("plan1", "proj1", json.dumps(plan_data), now),
        )

        decomposer = DecomposerService(db=tmp_db)
        result = await decomposer.decompose("proj1", "plan1")

        # Should have created 1 method task + 1 assembly task
        assert result["tasks_created"] >= 1

        # Check the method task has C# context
        task = await tmp_db.fetchone(
            "SELECT context_json FROM tasks WHERE project_id = ? AND task_type = 'csharp_method'",
            ("proj1",),
        )
        assert task is not None
        context = json.loads(task["context_json"])
        context_types = [c["type"] for c in context]
        assert "target_signature" in context_types
        assert "available_methods" in context_types
        assert "constructor_params" in context_types

        # Check signature content
        sig_entry = next(c for c in context if c["type"] == "target_signature")
        assert "GetUser" in sig_entry["content"]


class TestCreateCsharpAssemblyTasks:
    def test_creates_assembly_task_per_class(self):
        tasks_data = [
            {"task_type": "csharp_method", "target_class": "MyApp.UserService",
             "title": "GetUser", "affected_files": ["UserService.cs"]},
            {"task_type": "csharp_method", "target_class": "MyApp.UserService",
             "title": "SaveUser", "affected_files": ["UserService.cs"]},
            {"task_type": "csharp_method", "target_class": "MyApp.OrderService",
             "title": "CreateOrder", "affected_files": ["OrderService.cs"]},
        ]
        task_ids = ["t1", "t2", "t3"]
        waves = [0, 0, 0]
        phase_names = ["UserService", "UserService", "OrderService"]
        write_statements = []

        _create_csharp_assembly_tasks(
            tasks_data, task_ids, waves, phase_names,
            "proj1", "plan1", 1000.0, write_statements,
        )

        # Should have 2 assembly tasks (UserService, OrderService)
        # Each has 1 INSERT + N dependency INSERTs
        inserts = [s for s in write_statements if "INSERT INTO tasks" in s[0]]
        assert len(inserts) == 2

        # UserService assembly depends on t1 and t2
        dep_inserts = [s for s in write_statements if "INSERT INTO task_deps" in s[0]]
        user_deps = [s for s in dep_inserts if s[1][1] in ("t1", "t2")]
        assert len(user_deps) == 2

        # OrderService assembly depends on t3
        order_deps = [s for s in dep_inserts if s[1][1] == "t3"]
        assert len(order_deps) == 1

    def test_no_assembly_tasks_for_non_csharp(self):
        tasks_data = [
            {"task_type": "code", "title": "Do stuff"},
        ]
        task_ids = ["t1"]
        waves = [0]
        phase_names = [None]
        write_statements = []

        _create_csharp_assembly_tasks(
            tasks_data, task_ids, waves, phase_names,
            "proj1", "plan1", 1000.0, write_statements,
        )

        assert len(write_statements) == 0

    def test_assembly_wave_is_after_methods(self):
        tasks_data = [
            {"task_type": "csharp_method", "target_class": "Foo",
             "title": "M1", "affected_files": []},
            {"task_type": "csharp_method", "target_class": "Foo",
             "title": "M2", "affected_files": []},
        ]
        task_ids = ["t1", "t2"]
        waves = [0, 1]  # M2 is in wave 1
        phase_names = ["Foo", "Foo"]
        write_statements = []

        _create_csharp_assembly_tasks(
            tasks_data, task_ids, waves, phase_names,
            "proj1", "plan1", 1000.0, write_statements,
        )

        # Assembly task should be in wave 2 (max(0,1) + 1)
        insert = [s for s in write_statements if "INSERT INTO tasks" in s[0]][0]
        # wave is at index 12 in the VALUES tuple
        assembly_wave = insert[1][12]
        assert assembly_wave == 2

    def test_assembly_task_type(self):
        tasks_data = [
            {"task_type": "csharp_method", "target_class": "Foo",
             "title": "M1", "affected_files": []},
        ]
        task_ids = ["t1"]
        waves = [0]
        phase_names = ["Foo"]
        write_statements = []

        _create_csharp_assembly_tasks(
            tasks_data, task_ids, waves, phase_names,
            "proj1", "plan1", 1000.0, write_statements,
        )

        insert = [s for s in write_statements if "INSERT INTO tasks" in s[0]][0]
        # task_type is at index 5 in VALUES
        assert insert[1][5] == "csharp_assembly"
        # title should contain "Assemble"
        assert "Assemble" in insert[1][3]


class TestBuildVerification:
    async def test_build_success(self):
        from backend.services.task_lifecycle import verify_csharp_build

        with patch(
            "backend.tools.dotnet_reflection._run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, "Build succeeded.", ""),
        ):
            success, output = await verify_csharp_build("/fake/Test.csproj")
            assert success is True
            assert "succeeded" in output

    async def test_build_failure_extracts_errors(self):
        from backend.services.task_lifecycle import verify_csharp_build

        stderr = (
            "Program.cs(10,5): error CS1002: ; expected\n"
            "Program.cs(15,1): error CS0246: The type 'Foo' could not be found\n"
            "Build FAILED.\n"
        )
        with patch(
            "backend.tools.dotnet_reflection._run_subprocess",
            new_callable=AsyncMock,
            return_value=(1, "", stderr),
        ):
            success, output = await verify_csharp_build("/fake/Test.csproj")
            assert success is False
            assert "error CS1002" in output
            assert "error CS0246" in output
