"""P2 minimal persistent LOCAL mode (plan 043 rev 3; root msgs 1837/1873/1878). LOCAL development coordinator only.

The coordinator CREATES its own database and never adopts one:
  create(state_dir)  the single owned hekate-local container (labels, loopback) -> createdb of a NEW guarded name
                     hekate_coord_<utcstamp>_<hex> -> a marker row (random token) -> an exclusive locator file
                     {db, marker, projectId, apiPort} in state_dir -> the PlanStore schema (via the Api, as the
                     harness), the project row, the three journal installers (three transactions; not claimed atomic).
  open(state_dir)    the locator -> the database exists -> its marker row equals the locator -> the PlanStore and
                     journal schemas are complete -> bounds == E2C_BOUNDS -> the clock sanity rule. Only then a session.
Anything else is a typed LocalStoreRefused; there is no repair, migration, adoption or uninstall. stop() never drops
the database. Recovery after a failed create: drop that coordinator database and its locator, then create anew.

Time: journal records carry the wall-clock UTC epoch (LiveClockJournal); time.monotonic() is only for in-process
intervals. Across restarts only recorded UTC is compared, as a sanity STOP (backwards, or an implausible gap).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from e1 import harness as HZ
from e1.acts import E2C_BOUNDS
from e1.acts_durable import ActsJournal, install_acts
from e1.durable import bounds_of, install
from e1.handoff_durable import install_handoff

COORD_RE = re.compile(r"^hekate_coord_[0-9]{14}_[0-9a-f]{8}$")
LOCATOR = "coordinator.json"
PURPOSE = "hekate-local-development-coordinator"
MAX_GAP_S = 30 * 24 * 3600                    # an implausible gap since the last record stops (sanity, not a duration)
JOURNAL_TABLES = ("global_usage", "writer_usage", "streams", "records", "summaries", "takeovers",
                  "e2c_counters", "e2c_queue", "e2c_runs", "e2d_candidates")
JOURNAL_FUNCTIONS = ("guard_append_only", "guard_streams", "guard_e2c", "guard_e2d")


class LocalStoreRefused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code, self.detail = code, detail


class LiveClockJournal(ActsJournal):
    """The journal with a REAL clock. The journal reads `now` several times per record (its encoded size and its
    `at` must agree), so the value must be STABLE within an operation: it is SAMPLED from the wall clock (UTC epoch)
    whenever it is set (at construction, and on every pilot tick `now += dt`, whose arithmetic is ignored), and never
    goes backwards within the process. Recorded times are therefore real UTC sampled at the ticks, never a counter."""

    @property
    def now(self) -> float:
        return self._live

    @now.setter
    def now(self, _value: float) -> None:
        self._live = max(getattr(self, "_live", 0.0), time.time())


def clock_check(last_recorded_utc: float | None, now_utc: float, max_gap_s: float = MAX_GAP_S) -> None:
    """The cross-restart sanity rule: only recorded UTC is compared, never monotonic values."""
    if last_recorded_utc is None:
        return
    if now_utc < last_recorded_utc:
        raise LocalStoreRefused("clock_backwards", {"lastRecorded": last_recorded_utc, "now": now_utc})
    if now_utc - last_recorded_utc > max_gap_s:
        raise LocalStoreRefused("clock_gap_implausible", {"lastRecorded": last_recorded_utc, "now": now_utc, "maxGapS": max_gap_s})


@dataclass(frozen=True)
class Locator:
    db: str
    marker: str
    project_id: str
    api_port: int

    @staticmethod
    def read(state_dir: Path) -> "Locator":
        try:
            d = json.loads((Path(state_dir) / LOCATOR).read_text(encoding="utf-8"))
            loc = Locator(d["db"], d["marker"], d["projectId"], int(d["apiPort"]))
        except (OSError, ValueError, KeyError, TypeError):
            raise LocalStoreRefused("locator_unreadable", str(Path(state_dir) / LOCATOR)) from None
        if not COORD_RE.match(loc.db) or not re.fullmatch(r"[0-9a-f]{64}", loc.marker):
            raise LocalStoreRefused("locator_invalid", loc.db)
        uuid.UUID(loc.project_id)
        return loc


class LocalStore:
    def __init__(self, state_dir: Path, locator: Locator):
        self.state_dir, self.loc = Path(state_dir), locator
        self.base_url = f"http://127.0.0.1:{locator.api_port}"
        self.docker, self.dotnet = HZ._tool("docker"), HZ._tool("dotnet")
        self.container: str | None = None
        self.db_port: str | None = None
        self.api: subprocess.Popen[bytes] | None = None
        self.work = Path(state_dir) / "api"

    # -- the owned container (the harness pattern) -----------------------------------------------------------
    def _container(self) -> None:
        out = HZ._run([self.docker, "ps", "-q", "--no-trunc", "--filter", "label=com.docker.compose.project=hekate-local",
                       "--filter", f"label=com.hekate.local.workspace={HZ.container_workspace_label()}"]).stdout.split()
        if len(out) != 1:
            raise LocalStoreRefused("container", f"expected exactly one running owned hekate-local container, found {len(out)}")
        self.container = out[0]
        fmt = '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).%s}}'
        host = HZ._run([self.docker, "inspect", "--type", "container", "--format", fmt % "HostIp", self.container]).stdout.strip()
        if host != "127.0.0.1":
            raise LocalStoreRefused("container", f"database published on {host!r}, not loopback")
        self.db_port = HZ._run([self.docker, "inspect", "--type", "container", "--format", fmt % "HostPort", self.container]).stdout.strip()

    def psql(self, sql: str, db: str | None = None) -> str:
        p = HZ._run([self.docker, "exec", self.container, "psql", "-U", "postgres", "-d", db or self.loc.db,
                     "-v", "ON_ERROR_STOP=1", "-qtAc", sql])
        return p.stdout.strip()

    @property
    def dsn(self) -> str:
        return f"host=127.0.0.1 port={self.db_port} dbname={self.loc.db} user=postgres password=postgres"

    def _start_api(self) -> None:
        if not HZ._port_free(self.loc.api_port):
            raise LocalStoreRefused("api_port_in_use", self.loc.api_port)
        self.work.mkdir(parents=True, exist_ok=True)
        bin_dir = self.work / "bin"
        HZ._run([self.dotnet, "build", str(HZ.API_PROJECT), "-c", "Debug", "-o", str(bin_dir), "--nologo", "-v", "q"], timeout=600)
        env = dict(os.environ, CODESTORAGE_CONNSTR=f"Host=127.0.0.1;Port={self.db_port};Database={self.loc.db};Username=postgres;Password=postgres",
                   HEKATE_API_URLS=self.base_url, HEKATE_DISABLE_DISPATCHER="1", HEKATE_PLAN_CONTRACT="1")
        self._log = open(self.work / "api.log", "ab")
        self.api = subprocess.Popen([self.dotnet, str(bin_dir / "Api.dll")], cwd=str(HZ.API_PROJECT.parent), env=env,
                                    stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 90                                      # an in-process interval only
        while time.monotonic() < deadline:
            if self.api.poll() is not None:
                raise LocalStoreRefused("api_exited", self.api.returncode)
            try:
                with urllib.request.urlopen(self.base_url + "/api/health/ready", timeout=2) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.5)
        raise LocalStoreRefused("api_not_ready")

    # -- create / open / verify ------------------------------------------------------------------------------
    @classmethod
    def create(cls, state_dir: Path, *, api_port: int = 5109) -> "LocalStore":
        state_dir = Path(state_dir)
        if (state_dir / LOCATOR).exists():
            raise LocalStoreRefused("locator_exists", str(state_dir / LOCATOR))       # never adopt, never re-create in place
        db = f"hekate_coord_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"
        loc = Locator(db, secrets.token_hex(32), str(uuid.uuid4()), api_port)
        s = cls(state_dir, loc)
        s._container()
        HZ._run([s.docker, "exec", s.container, "createdb", "-U", "postgres", db])     # fails if the name exists
        s.psql(f"CREATE TABLE hekate_local_coordinator (marker text PRIMARY KEY, purpose text NOT NULL, project_id uuid NOT NULL, "
               f"created_at timestamptz NOT NULL DEFAULT now()); INSERT INTO hekate_local_coordinator (marker, purpose, project_id) "
               f"VALUES ('{loc.marker}', '{PURPOSE}', '{loc.project_id}')")
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / LOCATOR, "x", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"db": db, "marker": loc.marker, "projectId": loc.project_id, "apiPort": api_port}, indent=1))
        s.psql("CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS age; LOAD 'age'; "
               "SET search_path = ag_catalog, \"$user\", public; SELECT create_graph('code_graph'); "
               "SELECT create_vlabel('code_graph','CodeNode'); SELECT create_elabel('code_graph','DEPENDS_ON');")
        s._acquire_lock()                                                       # single instance from the start (review 1894 D3)
        s._start_api()                                                          # the Api applies the PlanStore schema
        s.psql(f"INSERT INTO projects (id, name, root_path) VALUES ('{loc.project_id}', 'local-coordinator', 'local://coordinator')")
        install(s.dsn, E2C_BOUNDS)
        install_acts(s.dsn)
        install_handoff(s.dsn)
        s.verify()
        return s

    @classmethod
    def open(cls, state_dir: Path) -> "LocalStore":
        loc = Locator.read(state_dir)
        s = cls(state_dir, loc)
        s._container()
        exists = s.psql(f"SELECT 1 FROM pg_database WHERE datname = '{loc.db}'", db="postgres")
        if exists != "1":
            raise LocalStoreRefused("db_missing", loc.db)
        s._check_marker()
        s._acquire_lock()                       # BEFORE the Api starts: a second coordinator is refused by name
        try:
            s._start_api()
        except BaseException:
            s.stop()
            raise
        try:
            s.verify()
            s.clock()
        except BaseException:
            s.stop()
            raise
        return s

    def _acquire_lock(self) -> None:
        """ONE coordinator per database (review 1894 D3): a PostgreSQL session advisory lock keyed by the marker,
        held on a dedicated connection for the store's lifetime and released by stop(). A second open is refused."""
        import psycopg
        key = int(self.loc.marker[:15], 16)                                  # < 2**60: a valid bigint key
        conn = psycopg.connect(self.dsn, autocommit=True)
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
            conn.close()
            raise LocalStoreRefused("store_in_use", self.loc.db)
        self._lock_conn = conn

    def _check_marker(self) -> None:
        try:
            row = self.psql("SELECT marker || '|' || purpose || '|' || project_id FROM hekate_local_coordinator")
        except subprocess.CalledProcessError:
            raise LocalStoreRefused("marker_missing", self.loc.db) from None
        if row != f"{self.loc.marker}|{PURPOSE}|{self.loc.project_id}":
            raise LocalStoreRefused("marker_mismatch", self.loc.db)

    def verify(self) -> None:
        """Complete BEFORE any dispatch: marker, PlanStore, the project, journal tables/functions, bounds."""
        self._check_marker()
        missing = [t for t in ("managed_plans", "projects") if self.psql(f"SELECT to_regclass('public.{t}') IS NOT NULL") != "t"]
        missing += [f"supervisor_journal.{t}" for t in JOURNAL_TABLES
                    if self.psql(f"SELECT to_regclass('supervisor_journal.{t}') IS NOT NULL") != "t"]
        have = set(self.psql("SELECT string_agg(p.proname, ',') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                             "WHERE n.nspname = 'supervisor_journal'").split(","))
        missing += [f"supervisor_journal.{f}()" for f in JOURNAL_FUNCTIONS if f not in have]
        if missing:
            raise LocalStoreRefused("schema_incomplete", missing)
        if self.psql(f"SELECT count(*) FROM projects WHERE id = '{self.loc.project_id}'") != "1":
            raise LocalStoreRefused("project_missing", self.loc.project_id)
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.dsn, row_factory=dict_row) as c:
            g = c.execute("SELECT * FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
        if g is None or bounds_of(g) != E2C_BOUNDS:
            raise LocalStoreRefused("bounds_mismatch", None if g is None else str(bounds_of(g)))

    def clock(self) -> None:
        last = self.psql("SELECT max(at_json::float8) FROM supervisor_journal.records")
        clock_check(float(last) if last else None, time.time())

    # -- a session ---------------------------------------------------------------------------------------------
    def session(self, *, actor: str, plan_bytes: bytes, writer: str | None = None) -> tuple[Any, Any, LiveClockJournal]:
        """(operator surface, supervisor client, live-clock journal) for import_plan / run_plan of ONE plan file. The
        operator policy's root allowlist is DERIVED from that validated file (root msg 1895)."""
        from e1 import plan_import as PI
        from e1.operator_acts import OperatorPolicy, OperatorSurface
        from e1.wire import SetupClient, SupervisorClient
        root = PI.identities(PI.parse(plan_bytes), self.loc.project_id).root
        setup = OperatorSurface(SetupClient(self.base_url), OperatorPolicy(actor, self.loc.project_id, frozenset({root})),
                                self.state_dir / "operator-acts.jsonl")
        left = setup.uncertain_acts()
        if left:                                     # stop BEFORE any dispatch; an operator classifies them (root msg 1904)
            raise LocalStoreRefused("uncertain_operator_acts", left[:10])
        aj = LiveClockJournal(self.dsn, writer or f"coordinator#{uuid.uuid4().hex[:8]}").open()
        return setup, SupervisorClient(self.base_url), aj

    def stop(self) -> None:
        """Stop the Api. The coordinator database is NEVER dropped here."""
        if self.api is not None and self.api.poll() is None:
            self.api.kill()
            self.api.wait(timeout=15)
        log = getattr(self, "_log", None)
        if log is not None:
            log.close()
        lock = getattr(self, "_lock_conn", None)
        if lock is not None:                                                     # releases the single-instance lock
            lock.close()
            self._lock_conn = None

    def drop_for_test(self) -> None:
        """TESTS ONLY: drop THIS coordinator database (guarded name + matching marker) and its locator."""
        self.stop()
        if not COORD_RE.match(self.loc.db):
            raise LocalStoreRefused("refused_drop", self.loc.db)
        HZ._run([self.docker, "exec", self.container, "dropdb", "-U", "postgres", "--force", self.loc.db], check=False, timeout=120)
        (self.state_dir / LOCATOR).unlink(missing_ok=True)
        shutil.rmtree(self.work, ignore_errors=True)
