"""Independent compatibility checks against PlanStore/viewer contracts."""
import pytest
pytest.importorskip('langchain_core')
pytest.importorskip('langgraph')
from e1.supervisor import Outcome
from test_role_supervisor import host, go


def test_viewer_exit_contract_is_normal_success(tmp_path):
    runner, client, journal, model = host(tmp_path)
    client.node['contentRevision'] = client.doc['receipt']['contentRevision']
    outcome = go(runner)
    assert outcome.outcome is Outcome.FINISHED
    exited = journal.one('exited')
    # AttemptTrace.ParseExited reads code, and treats any non-'exit' reason as a kill reason.
    assert exited.get('code') == 0
    assert exited.get('reason') == 'exit'


@pytest.mark.parametrize('value', ['missing', None, True])
def test_unverifiable_current_content_revision_prevents_model(tmp_path, value):
    runner, client, journal, model = host(tmp_path)
    if value == 'missing':
        client.node.pop('contentRevision', None)
    else:
        client.node['contentRevision'] = value
    outcome = go(runner)
    assert outcome.outcome is Outcome.NEEDS_OPERATOR
    assert model.calls == []
    assert client.finishes == []
