"""Shared test helpers (importable from every suite; conftest modules are per-directory)."""

import uuid


def key() -> str:
    return uuid.uuid4().hex


def ok(resp):
    assert resp.status == 200, (resp.status, resp.body)
    return resp.body
