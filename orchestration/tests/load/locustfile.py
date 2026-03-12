#  Orchestration Engine - Locust Load Test
#
#  HTTP load test using Locust. Simulates regular users browsing projects
#  and admin users checking budget/stats.
#
#  Usage:
#    locust -f tests/load/locustfile.py --host http://localhost:5200
#
#  Depends on: backend/routes (auth, projects, tasks, usage, services, admin)
#  Used by:    manual load testing

import os
import random
import string

from locust import HttpUser, between, task


def _random_email():
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"loadtest_{suffix}@test.com"


class OrchestratorUser(HttpUser):
    """Simulates a regular user: register, browse projects, create a project."""

    weight = 5
    wait_time = between(1, 3)

    def on_start(self):
        email = _random_email()
        password = "LoadTest1234!"
        display_name = "LoadUser"

        resp = self.client.post("/api/auth/register", json={
            "email": email,
            "password": password,
            "display_name": display_name,
        })
        if resp.status_code == 201:
            data = resp.json()
            self._token = data["access_token"]
        else:
            # Registration might fail if email collides — try login
            resp = self.client.post("/api/auth/login", json={
                "email": email, "password": password,
            })
            data = resp.json()
            self._token = data["access_token"]

        self._headers = {"Authorization": f"Bearer {self._token}"}

    @task(5)
    def list_projects(self):
        self.client.get("/api/projects", headers=self._headers)

    @task(2)
    def create_project(self):
        self.client.post("/api/projects", json={
            "name": f"Load Test Project {random.randint(1, 10000)}",
            "requirements": "This is a load test project with sample requirements.",
        }, headers=self._headers)

    @task(3)
    def get_project_detail(self):
        resp = self.client.get("/api/projects", headers=self._headers)
        if resp.status_code == 200:
            projects = resp.json()
            if projects:
                pid = random.choice(projects)["id"]
                self.client.get(f"/api/projects/{pid}", headers=self._headers)

    @task(1)
    def get_usage(self):
        resp = self.client.get("/api/projects", headers=self._headers)
        if resp.status_code == 200:
            projects = resp.json()
            if projects:
                pid = random.choice(projects)["id"]
                self.client.get(
                    f"/api/usage/summary?project_id={pid}",
                    headers=self._headers,
                )

    @task(1)
    def list_services(self):
        self.client.get("/api/services", headers=self._headers)


class AdminUser(HttpUser):
    """Simulates an admin user: checks budget, stats, user list."""

    weight = 1
    wait_time = between(2, 5)

    def on_start(self):
        # Use the LOAD_TEST_ADMIN env vars, or register (first user = admin)
        email = os.environ.get("LOAD_TEST_ADMIN_EMAIL", "admin_load@test.com")
        password = os.environ.get("LOAD_TEST_ADMIN_PASSWORD", "AdminLoad1234!")

        # Try login first (admin might already exist)
        resp = self.client.post("/api/auth/login", json={
            "email": email, "password": password,
        })
        if resp.status_code == 200:
            self._token = resp.json()["access_token"]
        else:
            # Register (first user is admin)
            resp = self.client.post("/api/auth/register", json={
                "email": email,
                "password": password,
                "display_name": "Admin",
            })
            self._token = resp.json()["access_token"]

        self._headers = {"Authorization": f"Bearer {self._token}"}

    @task(1)
    def check_budget(self):
        self.client.get("/api/usage/summary", headers=self._headers)

    @task(1)
    def check_daily(self):
        self.client.get("/api/usage/daily", headers=self._headers)

    @task(1)
    def admin_stats(self):
        self.client.get("/api/admin/stats", headers=self._headers)

    @task(1)
    def admin_users(self):
        self.client.get("/api/admin/users", headers=self._headers)
