"""Opt-in live H1 case (plan 023 E1b): `uv run pytest interop_live`. Needs the owned
hekate-local container AND the pinned ChatAgent checkout; either missing ERRORS (never skips)."""

import pytest

from e1.h1_bridge import verify_checkout
from e1.harness import Harness
from e1.wire import SetupClient, SupervisorClient


@pytest.fixture(scope="session")
def harness(request):
    verify_checkout()
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
