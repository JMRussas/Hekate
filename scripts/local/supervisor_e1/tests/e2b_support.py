"""Shared E2b-a fixtures (TEST-ONLY). The journal schema is (re)created per test inside the harness's
disposable database, so every test starts from empty journal tables with its own bounds."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from e1.durable import LOCK_NS, DurableJournal, install
from e1.evidence import Bounds, JournalRefused

W = "supervisor-e1#A"
PROJECT = Path(__file__).resolve().parents[1]
TABLES = ("global_usage", "writer_usage", "streams", "records", "summaries", "takeovers")


def reset_schema(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        # Only sessions holding a JOURNAL writer lock in THIS disposable database (leftover children).
        c.execute("SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' AND classid = %s::oid "
                  "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) AND pid <> pg_backend_pid()",
                  (LOCK_NS,))
        c.execute("DROP SCHEMA IF EXISTS supervisor_journal CASCADE")


def journal_digest(dsn: str) -> str:
    """Content digest of every journal table (no-write assertions)."""
    parts = " || '#' || ".join(
        f"coalesce((SELECT string_agg(t::text, '|' ORDER BY t::text) FROM supervisor_journal.{t} t), '')" for t in TABLES)
    with psycopg.connect(dsn) as c:
        return c.execute(f"SELECT md5({parts})").fetchone()[0]


def raw(dsn: str, *stmts, replica: bool = False):
    """A separate credentialed session running each statement (str or (sql, params)) in ONE
    transaction (tamper / guard probes). replica=True disables triggers for the session."""
    rows = None
    with psycopg.connect(dsn, autocommit=False) as c:
        if replica:
            c.execute("SET session_replication_role = replica")
        for st in stmts:
            sql, params = (st, None) if isinstance(st, str) else st
            cur = c.execute(sql, params)
            rows = cur.fetchall() if cur.description else None
        c.commit()
    return rows


@pytest.fixture
def jdb(harness):
    opened: list[DurableJournal] = []

    def make(bounds: Bounds | None = None) -> str:
        reset_schema(harness.dsn)
        install(harness.dsn, bounds or Bounds())
        return harness.dsn

    def writer(name: str = W, *, now: float = 0.0, **kw) -> DurableJournal:
        dj = DurableJournal(harness.dsn, name, now=now, **kw)
        opened.append(dj)
        return dj.open()

    yield SimpleNamespace(make=make, writer=writer, dsn=harness.dsn)
    for dj in opened:
        dj.close()


class Child:
    """A separate Python process with its OWN database session (killed at crash points)."""

    def __init__(self):
        self.p = subprocess.Popen([sys.executable, "-m", "e1.journal_child"], cwd=str(PROJECT), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)

    def send(self, **cmd):
        self.p.stdin.write(json.dumps(cmd) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError(f"child exited ({self.p.poll()})")
        return json.loads(line)

    def kill(self) -> None:
        self.p.kill()
        self.p.wait(timeout=15)
        for s in (self.p.stdin, self.p.stdout):
            try:
                s.close()
            except OSError:
                pass


def backend_gone(dsn: str, pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with psycopg.connect(dsn) as c:
            if c.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()[0] == 0:
                return True
        time.sleep(0.2)
    return False


def open_when_free(dsn: str, writer: str = W, timeout: float = 15.0) -> DurableJournal:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return DurableJournal(dsn, writer).open()
        except JournalRefused as e:
            if e.code != "fence_busy" or time.monotonic() > deadline:
                raise
            time.sleep(0.2)
