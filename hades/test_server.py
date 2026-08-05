"""Tests for the Hades admin server health endpoint."""

from fastapi.testclient import TestClient

from server import app


client = TestClient(app)


def test_detailed_health_returns_200_with_expected_keys():
    response = client.get("/api/health/detailed")
    assert response.status_code == 200

    data = response.json()
    assert "service_version" in data
    assert "uptime_seconds" in data
    assert "database_connection_status" in data
    assert "current_timestamp" in data
