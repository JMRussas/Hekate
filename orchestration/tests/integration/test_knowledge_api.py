#  Orchestration Engine - Knowledge API Tests
#
#  Integration tests for GET/DELETE /projects/{id}/knowledge endpoints.
#
#  Depends on: conftest.py fixtures
#  Used by:    CI pipeline

import hashlib
import time

import pytest


async def _seed_knowledge_via_db(project_id: str, entries: list[dict]):
    """Insert knowledge entries directly via the DI container's DB."""
    from backend.app import container

    db = container.db()
    now = time.time()
    for i, entry in enumerate(entries):
        content = entry["content"]
        category = entry.get("category", "discovery")
        content_hash = hashlib.sha256(content.lower().encode()).hexdigest()[:32]
        await db.execute_write(
            "INSERT INTO project_knowledge "
            "(id, project_id, task_id, category, content, content_hash, "
            "source_task_title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"finding_{i}", project_id, None, category, content,
             content_hash, f"Task {i}", now - i),
        )


class TestKnowledgeAPI:
    """Integration tests for knowledge endpoints."""

    @pytest.mark.asyncio
    async def test_list_knowledge(self, authed_client):
        """GET /knowledge returns findings for the project."""
        # Create a project
        resp = await authed_client.post("/api/projects", json={
            "name": "Knowledge Test",
            "requirements": "Build something",
        })
        assert resp.status_code == 201
        project_id = resp.json()["id"]

        # Seed knowledge directly
        await _seed_knowledge_via_db(project_id, [
            {"category": "constraint", "content": "Must use Python 3.11+"},
            {"category": "gotcha", "content": "SQLite WAL mode required"},
        ])

        # List knowledge
        resp = await authed_client.get(f"/api/projects/{project_id}/knowledge")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        categories = {f["category"] for f in data}
        assert "constraint" in categories
        assert "gotcha" in categories

    @pytest.mark.asyncio
    async def test_list_knowledge_filter_by_category(self, authed_client):
        """GET /knowledge?category=constraint filters results."""
        resp = await authed_client.post("/api/projects", json={
            "name": "Filter Test",
            "requirements": "Build something",
        })
        project_id = resp.json()["id"]

        await _seed_knowledge_via_db(project_id, [
            {"category": "constraint", "content": "Only constraint"},
            {"category": "gotcha", "content": "Only gotcha"},
        ])

        resp = await authed_client.get(
            f"/api/projects/{project_id}/knowledge?category=constraint"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["category"] == "constraint"

    @pytest.mark.asyncio
    async def test_delete_finding(self, authed_client):
        """DELETE /knowledge/{id} removes the finding."""
        resp = await authed_client.post("/api/projects", json={
            "name": "Delete Test",
            "requirements": "Build something",
        })
        project_id = resp.json()["id"]

        await _seed_knowledge_via_db(project_id, [
            {"category": "discovery", "content": "Will be deleted"},
        ])

        # Verify it exists
        resp = await authed_client.get(f"/api/projects/{project_id}/knowledge")
        assert len(resp.json()) == 1
        finding_id = resp.json()[0]["id"]

        # Delete it
        resp = await authed_client.delete(
            f"/api/projects/{project_id}/knowledge/{finding_id}"
        )
        assert resp.status_code == 204

        # Verify it's gone
        resp = await authed_client.get(f"/api/projects/{project_id}/knowledge")
        assert len(resp.json()) == 0

    @pytest.mark.asyncio
    async def test_delete_finding_not_found(self, authed_client):
        """DELETE /knowledge/{bad_id} returns 404."""
        resp = await authed_client.post("/api/projects", json={
            "name": "Not Found Test",
            "requirements": "Build something",
        })
        project_id = resp.json()["id"]

        resp = await authed_client.delete(
            f"/api/projects/{project_id}/knowledge/nonexistent"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_project_delete_cascades_knowledge(self, authed_client):
        """Deleting a project cascades to its knowledge entries."""
        from backend.app import container

        resp = await authed_client.post("/api/projects", json={
            "name": "Cascade Test",
            "requirements": "Build something",
        })
        project_id = resp.json()["id"]

        await _seed_knowledge_via_db(project_id, [
            {"category": "discovery", "content": "Will be cascade-deleted"},
        ])

        # Delete the project
        resp = await authed_client.delete(f"/api/projects/{project_id}")
        assert resp.status_code == 204

        # Verify knowledge is gone (query DB directly)
        db = container.db()
        rows = await db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", (project_id,)
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_knowledge_in_export(self, authed_client):
        """GET /export includes knowledge entries."""
        resp = await authed_client.post("/api/projects", json={
            "name": "Export Test",
            "requirements": "Build something",
        })
        project_id = resp.json()["id"]

        await _seed_knowledge_via_db(project_id, [
            {"category": "architecture", "content": "Uses event-driven pattern"},
        ])

        resp = await authed_client.get(f"/api/projects/{project_id}/export")
        assert resp.status_code == 200
        data = resp.json()
        assert "knowledge" in data
        assert len(data["knowledge"]) == 1
        assert data["knowledge"][0]["content"] == "Uses event-driven pattern"
