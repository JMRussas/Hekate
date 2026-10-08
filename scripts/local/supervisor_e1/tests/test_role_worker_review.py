"""Independent lead regressions for the role execution trust boundary."""
import asyncio
import json
from dataclasses import replace

import pytest
pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from e1.role_worker import RoleError, athena_role, run_role


class Model(BaseChatModel):
    response: str
    _calls: int = PrivateAttr(default=0)

    @property
    def _llm_type(self):
        return "lead-review-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("only async invocation expected")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])


def inputs():
    return {"evidence": [{"id": "source-1", "text": "observed evidence"}]}, {
        "rootId": "29141a72-9c9a-54f8-a357-fb6db74d84d9",
        "taskId": "a08ee7ab-7b4a-5bde-967e-eb512132d089",
        "attemptId": "review-r1", "epoch": 1, "contentRevision": 1,
        "observedAt": "2026-10-08T21:00:00Z",
    }


def model():
    return Model(response=json.dumps({"summary": "review", "steps": [
        {"title": "test", "acceptance": "passes", "evidence": ["source-1"]}], "findings": []}))


@pytest.mark.parametrize("field,value", [("epoch", 0), ("contentRevision", 0),
                                         ("rootId", "not-a-uuid"), ("taskId", "not-a-uuid"),
                                         ("attemptId", "attempt\n")])
def test_bad_correlation_refused_before_model(field, value):
    snapshot, correlation = inputs()
    correlation[field] = value
    provider = model()
    result = asyncio.run(run_role(athena_role(), provider, snapshot, correlation))
    assert result.status == "failed"
    assert result.failure_code == "input_invalid"
    assert provider._calls == 0


def test_unhashable_binding_config_is_typed_refusal():
    with pytest.raises(RoleError) as error:
        replace(athena_role(), allowed_bindings=(["bad"],))
    assert error.value.code == "config_invalid"


def test_escaped_invalid_unicode_is_typed_output_refusal():
    snapshot, correlation = inputs()
    provider = model()
    document = json.loads(provider.response)
    document["summary"] = "\ud800"
    provider.response = json.dumps(document)
    result = asyncio.run(run_role(athena_role(), provider, snapshot, correlation))
    assert result.status == "failed"
    assert result.failure_code in {"malformed_output", "schema_invalid"}
    assert not result.accepted
