import uuid

import pytest

from e1.harness import Harness
from e1.wire import SetupClient, SupervisorClient


@pytest.fixture(scope="session")
def harness(request):
    h = Harness()
    h.start()
    yield h
    problems = h.stop(keep_work=request.session.testsfailed > 0)
    if problems and not (request.session.testsfailed > 0 and all(p.startswith("work folder kept") for p in problems)):
        pytest.fail("harness cleanup: " + "; ".join(problems))


@pytest.fixture
def setup(harness) -> SetupClient:
    return SetupClient(harness.base_url)


@pytest.fixture
def client(harness) -> SupervisorClient:
    return SupervisorClient(harness.base_url)


def key() -> str:
    return uuid.uuid4().hex


def ok(resp):
    assert resp.status == 200, (resp.status, resp.body)
    return resp.body
