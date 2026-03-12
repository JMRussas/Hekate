#  Orchestration Engine - Test Fixtures
#
#  Shared fixtures for the test suite.
#  Uses DI container overrides instead of monkey-patching singletons.
#
#  Depends on: backend/db/connection.py, backend/container.py, backend/app.py
#  Used by:    all test files

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dependency_injector import providers


# ---------------------------------------------------------------------------
# Global cleanup fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_model_warnings():
    """Reset warned-models set between tests to prevent state leak."""
    from backend.services.model_router import _reset_warned_models
    _reset_warned_models()
    yield
    _reset_warned_models()


# ---------------------------------------------------------------------------
# Database fixture
# ---------------------------------------------------------------------------

@pytest.fixture
async def tmp_db(tmp_path):
    """Create a fresh async database with schema applied."""
    from backend.db.connection import Database

    test_db = Database()
    db_path = tmp_path / "test.db"
    await test_db.init(str(db_path))

    yield test_db

    await test_db.close()


@pytest.fixture
async def seeded_db(tmp_db):
    """Database with a sample project and draft plan (2 tasks, 1 dependency)."""
    now = time.time()
    project_id = "proj_test_001"
    plan_id = "plan_test_001"

    await tmp_db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'draft', ?, ?)",
        (project_id, "Test Project", "Build a test thing", now, now),
    )

    plan_data = {
        "summary": "Test plan",
        "tasks": [
            {
                "title": "Task A",
                "description": "Do A",
                "task_type": "code",
                "complexity": "simple",
                "depends_on": [],
                "tools_needed": [],
            },
            {
                "title": "Task B",
                "description": "Do B",
                "task_type": "research",
                "complexity": "simple",
                "depends_on": [0],
                "tools_needed": [],
            },
        ],
    }
    await tmp_db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test-model', ?, 'draft', ?)",
        (plan_id, project_id, json.dumps(plan_data), now),
    )

    return tmp_db, project_id, plan_id


# ---------------------------------------------------------------------------
# Shared FK-safe data helpers
# ---------------------------------------------------------------------------

async def create_test_project(db, project_id="proj1"):
    """Insert a project + plan row so FK constraints pass."""
    now = time.time()
    await db.execute_write(
        "INSERT OR IGNORE INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, 'Test', 'test', 'draft', ?, ?)",
        (project_id, now, now),
    )
    await db.execute_write(
        "INSERT OR IGNORE INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
        (f"plan_{project_id}", project_id, now),
    )


async def create_test_task(db, task_id, project_id="proj1"):
    """Insert a task row so FK constraints pass (project must exist)."""
    now = time.time()
    await db.execute_write(
        "INSERT OR IGNORE INTO tasks (id, project_id, plan_id, title, description, "
        "task_type, priority, status, model_tier, wave, retry_count, max_retries, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, 'Test', 'test', 'code', 0, 'pending', 'haiku', 0, 0, 5, ?, ?)",
        (task_id, project_id, f"plan_{project_id}", now, now),
    )


# ---------------------------------------------------------------------------
# Auth fixture
# ---------------------------------------------------------------------------

@pytest.fixture
async def auth_service(tmp_db):
    """AuthService wired to the test database."""
    from backend.services.auth import AuthService
    return AuthService(db=tmp_db)


# ---------------------------------------------------------------------------
# FastAPI TestClient fixture
# ---------------------------------------------------------------------------

@pytest.fixture
async def app_client(tmp_db):
    """FastAPI TestClient with a fresh database. Uses DI container overrides.

    Uses explicit try/finally with reset_override() instead of context managers
    to ensure DI state is fully cleaned up — async fixture teardown can have
    edge cases where nested context managers don't exit cleanly in full suite runs.
    """
    from httpx import ASGITransport, AsyncClient
    from backend.app import app, container
    from backend.services.auth import AuthService
    from backend.services.budget import BudgetManager
    from backend.services.progress import ProgressManager

    mock_executor = MagicMock()
    mock_executor.start = AsyncMock()
    mock_executor.stop = AsyncMock()

    mock_rm = MagicMock()
    mock_rm.start_background = AsyncMock()
    mock_rm.stop_background = AsyncMock()
    mock_rm.check_all = AsyncMock(return_value=[])

    mock_http = AsyncMock()
    mock_http.aclose = AsyncMock()

    from backend.services.oidc import OIDCService

    auth = AuthService(db=tmp_db)
    oidc = OIDCService(db=tmp_db, auth=auth)
    budget = BudgetManager(db=tmp_db)
    progress = ProgressManager(db=tmp_db)

    init_patcher = patch.object(tmp_db, "init", new_callable=AsyncMock)

    container.db.override(providers.Object(tmp_db))
    container.auth.override(providers.Object(auth))
    container.oidc.override(providers.Object(oidc))
    container.budget.override(providers.Object(budget))
    container.progress.override(providers.Object(progress))
    container.executor.override(providers.Object(mock_executor))
    container.resource_monitor.override(providers.Object(mock_rm))
    container.http_client.override(providers.Object(mock_http))
    init_patcher.start()

    # Reset rate limiter storage so tests don't hit limits from prior tests
    from backend.rate_limit import limiter as _limiter
    _limiter.reset()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        init_patcher.stop()
        container.db.reset_override()
        container.auth.reset_override()
        container.oidc.reset_override()
        container.budget.reset_override()
        container.progress.reset_override()
        container.executor.reset_override()
        container.resource_monitor.reset_override()
        container.http_client.reset_override()


@pytest.fixture
async def authed_client(app_client):
    """app_client with a registered user and Authorization header set."""
    # Register a test user
    resp = await app_client.post("/api/auth/register", json={
        "email": "test@example.com",
        "password": "testpass123",
        "display_name": "Test User",
    })
    assert resp.status_code == 201

    # Login to get tokens
    resp = await app_client.post("/api/auth/login", json={
        "email": "test@example.com",
        "password": "testpass123",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    # Set default auth header
    app_client.headers["Authorization"] = f"Bearer {token}"
    yield app_client


# ---------------------------------------------------------------------------
# Mock Anthropic client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_anthropic():
    """Mocked anthropic.AsyncAnthropic that returns canned plan responses."""
    mock_client = AsyncMock()

    plan_response = MagicMock()
    plan_response.content = [
        MagicMock(
            text=json.dumps({
                "summary": "Mock plan",
                "tasks": [
                    {
                        "title": "Mock Task",
                        "description": "Do mock thing",
                        "task_type": "code",
                        "complexity": "simple",
                        "depends_on": [],
                        "tools_needed": [],
                    },
                ],
            }),
            type="text",
        )
    ]
    plan_response.usage = MagicMock(input_tokens=100, output_tokens=200)

    mock_client.messages.create = AsyncMock(return_value=plan_response)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        yield mock_client


# ---------------------------------------------------------------------------
# Mock Ollama
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ollama():
    """Mocked httpx calls for Ollama."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "response": "Mock Ollama output",
        "prompt_eval_count": 50,
        "eval_count": 100,
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_class:
        mock_instance = AsyncMock()
        mock_instance.post = AsyncMock(return_value=mock_response)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock()
        mock_class.return_value = mock_instance
        yield mock_instance
