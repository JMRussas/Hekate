"""Disposable database + Api process for E1a (test-only; mirrors PlanContractApi.Tests.ps1).

Owns ONLY what it creates: one new hekate_plan_e1_* database in the owned hekate-local
container and the Api process objects it started. Cleanup runs even when startup fails, is
verified, and never masks the original exception (cleanup problems are attached as notes).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import uuid
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
API_PROJECT = REPO / "context-store" / "Api" / "Api.csproj"
DB_NAME_RE = re.compile(r"^hekate_plan_e1_[0-9]{14}_[0-9a-f]{8}$")
# The owned container is labelled with the ORIGINAL workspace path. A clean-checkout review may
# build from another folder (REPO) but must still name this container explicitly; arbitrary
# container discovery is never allowed.
ORIGINAL_WORKSPACE = r"D:\Git\Hekate"


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def container_workspace_label() -> str:
    """Label used to find the owned container: HEKATE_E1_CONTAINER_WORKSPACE (validated) or this repo."""
    override = os.environ.get("HEKATE_E1_CONTAINER_WORKSPACE")
    if override is None:
        return str(REPO)
    if _norm(override) not in (_norm(ORIGINAL_WORKSPACE), _norm(str(REPO))):
        raise RuntimeError(f"HEKATE_E1_CONTAINER_WORKSPACE={override!r} is neither {ORIGINAL_WORKSPACE} nor this repo")
    return ORIGINAL_WORKSPACE if _norm(override) == _norm(ORIGINAL_WORKSPACE) else str(REPO)


def _tool(name: str) -> str:
    """Resolve an executable explicitly (PATH + PATHEXT on Windows); fail loudly if absent."""
    found = shutil.which(name)
    if not found and name == "docker":
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
        found = str(candidate) if candidate.exists() else None
    if not found:
        raise RuntimeError(f"{name} not found on PATH (PATHEXT={os.environ.get('PATHEXT')!r})")
    return found


def _run(args: list[str], *, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}... failed ({p.returncode}): {p.stderr.strip()[:400]}")
    return p


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


class Harness:
    def __init__(self, port: int = 5108):
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.docker = _tool("docker")
        self.dotnet = _tool("dotnet")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        self.db = f"hekate_plan_e1_{stamp}_{secrets.token_hex(4)}"
        if not DB_NAME_RE.match(self.db):
            raise RuntimeError(f"database name guard failed: {self.db}")
        self.work = Path(__file__).resolve().parents[1] / ".run" / secrets.token_hex(8)
        self.container: str | None = None
        self.db_created = False
        self.api: subprocess.Popen[bytes] | None = None
        self.project_id: str | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        try:
            self._start()
        except BaseException as original:
            notes = self.stop(keep_work=True)
            for n in notes:
                original.add_note(f"cleanup: {n}")
            raise

    def _start(self) -> None:
        if not _port_free(self.port):
            raise RuntimeError(f"port {self.port} is in use; refusing")
        repo_label = container_workspace_label()
        out = _run([self.docker, "ps", "-q", "--no-trunc",
                    "--filter", "label=com.docker.compose.project=hekate-local",
                    "--filter", f"label=com.hekate.local.workspace={repo_label}"]).stdout.split()
        if len(out) != 1:
            raise RuntimeError(f"expected exactly one running owned hekate-local container, found {len(out)}")
        self.container = out[0]
        host_ip = _run([self.docker, "inspect", "--type", "container", "--format",
                        '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostIp}}', self.container]).stdout.strip()
        db_port = _run([self.docker, "inspect", "--type", "container", "--format",
                        '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}', self.container]).stdout.strip()
        if host_ip != "127.0.0.1":
            raise RuntimeError(f"database published on {host_ip!r}, not loopback")

        _run([self.docker, "exec", self.container, "createdb", "-U", "postgres", self.db])
        self.db_created = True
        self.psql("CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS age; LOAD 'age'; "
                  "SET search_path = ag_catalog, \"$user\", public; SELECT create_graph('code_graph'); "
                  "SELECT create_vlabel('code_graph','CodeNode'); SELECT create_elabel('code_graph','DEPENDS_ON');")

        self.work.mkdir(parents=True, exist_ok=True)
        bin_dir = self.work / "api-bin"
        _run([self.dotnet, "build", str(API_PROJECT), "-c", "Debug", "-o", str(bin_dir), "--nologo", "-v", "q"], timeout=600)

        env = dict(os.environ)
        env.update({
            "CODESTORAGE_CONNSTR": f"Host=127.0.0.1;Port={db_port};Database={self.db};Username=postgres;Password=postgres",
            "HEKATE_API_URLS": self.base_url,
            "HEKATE_DISABLE_DISPATCHER": "1",
            "HEKATE_PLAN_CONTRACT": "1",
        })
        self._log = open(self.work / "api.log", "wb")
        self.api = subprocess.Popen([self.dotnet, str(bin_dir / "Api.dll")], cwd=str(API_PROJECT.parent), env=env,
                                    stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.api.poll() is not None:
                raise RuntimeError(f"Api exited early with {self.api.returncode}; see {self.work / 'api.log'}")
            try:
                with urllib.request.urlopen(self.base_url + "/api/health/ready", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Api did not become ready within 90s")

        self.project_id = str(uuid.uuid4())
        self.psql(f"INSERT INTO projects (id, name, root_path) VALUES ('{self.project_id}', 'supervisor-e1', 'disposable://supervisor-e1')")

    def stop(self, *, keep_work: bool = False) -> list[str]:
        """Release only what this harness created. Returns cleanup problems (empty when clean)."""
        # Every step is independent; nothing here raises, so an original error is never masked.
        problems: list[str] = []
        api_gone = self.api is None
        if self.api is not None:
            try:
                if self.api.poll() is None:
                    self.api.kill()
                    self.api.wait(timeout=15)
            except Exception as e:  # noqa: BLE001
                problems.append(f"Api process {self.api.pid}: {type(e).__name__}: {e}")
            try:
                api_gone = self.api.poll() is not None
            except Exception as e:  # noqa: BLE001
                problems.append(f"Api process {self.api.pid} state: {e}")
            if not api_gone:
                problems.append(f"Api process {self.api.pid} did not exit")
        log = getattr(self, "_log", None)
        if log is not None:
            try:
                log.close()
            except Exception as e:  # noqa: BLE001
                problems.append(f"closing api.log: {e}")
        db_gone = not self.db_created
        if self.db_created and self.container:
            if not DB_NAME_RE.match(self.db):
                problems.append(f"refused to drop unexpected database {self.db}")
            else:
                try:
                    p = _run([self.docker, "exec", self.container, "dropdb", "-U", "postgres", "--force", self.db], check=False, timeout=120)
                    left = _run([self.docker, "exec", self.container, "psql", "-U", "postgres", "-d", "postgres", "-qtAc",
                                 f"SELECT 1 FROM pg_database WHERE datname = '{self.db}'"], check=False, timeout=60)
                    if p.returncode != 0 or left.returncode != 0 or left.stdout.strip() == "1":
                        problems.append(f"dropdb {self.db} failed (exit {p.returncode}, verify exit {left.returncode})")
                    else:
                        self.db_created = False
                        db_gone = True
                except Exception as e:  # noqa: BLE001 (timeouts, OSError)
                    problems.append(f"dropdb {self.db}: {type(e).__name__}: {e}")
        # Generated work (build output, api.log) is deleted only after a confirmed Api exit and DB
        # drop with no problems; otherwise it is kept for diagnosis.
        if api_gone and db_gone and not problems and not keep_work:
            shutil.rmtree(self.work, ignore_errors=True)
            try:
                self.work.parent.rmdir()   # only when empty (another run may share it)
            except OSError:
                pass
        elif self.work.exists():
            problems.append(f"work folder kept for diagnosis: {self.work}")
        return problems

    # --- raw database access (setup / assertions only) ---------------------

    def psql(self, sql: str) -> str:
        if not self.container:
            raise RuntimeError("container not resolved")
        p = _run([self.docker, "exec", self.container, "psql", "-U", "postgres", "-d", self.db,
                  "-v", "ON_ERROR_STOP=1", "-qtAc", sql])
        return p.stdout.strip()
