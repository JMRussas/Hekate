#  Orchestration Engine - C# Worker Prompt Tests
#
#  Tests for the WorkerInstruction prompt builder in claude_agent.py.
#
#  Depends on: backend/services/claude_agent.py
#  Used by:    CI

from backend.services.claude_agent import (
    _build_csharp_worker_prompt,
    _extract_csharp_context,
)


class TestExtractCsharpContext:
    def test_extracts_csharp_fields(self):
        context = [
            {"type": "project_summary", "content": "A project"},
            {"type": "target_signature", "content": "public async Task<User> GetUser(Guid id)"},
            {"type": "available_methods", "content": "public void Save(User u)\npublic User Find(Guid id)"},
            {"type": "constructor_params", "content": "IDbContext db, ILogger logger"},
        ]
        result = _extract_csharp_context(context)
        assert result is not None
        assert result["target_signature"] == "public async Task<User> GetUser(Guid id)"
        assert "Save" in result["available_methods"]
        assert "IDbContext" in result["constructor_params"]

    def test_returns_none_without_target_signature(self):
        context = [
            {"type": "project_summary", "content": "A project"},
            {"type": "available_methods", "content": "some methods"},
        ]
        result = _extract_csharp_context(context)
        assert result is None

    def test_returns_none_for_empty_context(self):
        assert _extract_csharp_context([]) is None

    def test_minimal_csharp_context(self):
        context = [{"type": "target_signature", "content": "public void DoStuff()"}]
        result = _extract_csharp_context(context)
        assert result is not None
        assert "target_signature" in result


class TestBuildCsharpWorkerPrompt:
    def test_includes_worker_instruction_tags(self):
        ctx = {
            "target_signature": "public async Task<bool> ValidateUser(Guid id)",
            "available_methods": "public User Find(Guid id)",
            "constructor_params": "IUserStore store",
        }
        task_row = {"description": "Check if user exists and has active subscription"}
        prompt = _build_csharp_worker_prompt(ctx, task_row)

        assert "<WorkerInstruction>" in prompt
        assert "</WorkerInstruction>" in prompt
        assert "<TargetSignature>" in prompt
        assert "ValidateUser" in prompt

    def test_includes_logic_goal(self):
        ctx = {"target_signature": "public void Foo()"}
        task_row = {"description": "Do the foo thing"}
        prompt = _build_csharp_worker_prompt(ctx, task_row)
        assert "<LogicGoal>" in prompt
        assert "Do the foo thing" in prompt

    def test_includes_execution_rules(self):
        ctx = {"target_signature": "public void Foo()"}
        task_row = {"description": "test"}
        prompt = _build_csharp_worker_prompt(ctx, task_row)
        assert "ONLY the method body" in prompt
        assert "Do not hallucinate" in prompt
        assert "50 lines" in prompt

    def test_includes_dependencies(self):
        ctx = {
            "target_signature": "public void Foo()",
            "constructor_params": "IDbContext db, ILogger log",
            "available_methods": "public void Bar()\npublic int Baz()",
        }
        task_row = {"description": "test"}
        prompt = _build_csharp_worker_prompt(ctx, task_row)
        assert "<InjectedDependencies>" in prompt
        assert "IDbContext db" in prompt
        assert "<AvailableMethods>" in prompt
        assert "Bar()" in prompt
        assert "Baz()" in prompt

    def test_handles_missing_optional_fields(self):
        ctx = {"target_signature": "public void Foo()"}
        task_row = {"description": "test"}
        prompt = _build_csharp_worker_prompt(ctx, task_row)
        # Should still produce valid XML without crashing
        assert "<TargetSignature>public void Foo()</TargetSignature>" in prompt
        assert "None" in prompt  # default for missing fields
