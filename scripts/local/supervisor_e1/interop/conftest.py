"""Opt-in H1 interop (plan 023 E1b): `uv run pytest interop`. Not part of the default suite.

The pinned ChatAgent checkout and runtime are verified once; a missing, dirty or wrong checkout
ERRORS the suite (it never skips).
"""

import pytest

from e1.h1_bridge import H1_COMMIT, verify_checkout


@pytest.fixture(scope="session", autouse=True)
def h1_checkout():
    repo = verify_checkout()   # raises H1Unavailable -> every test errors, nothing is skipped
    return repo, H1_COMMIT
