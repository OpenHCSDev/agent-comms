"""A remote call keeps the error owner's refusal and recovery roles."""

import pytest
from yarl import URL

from agent_comms.coordination.errors import CoordinationReadUnavailable
from agent_comms.errors import RelationViolationError
from agent_comms.storage.maintenance_barrier import MaintenanceHeldError
from agent_comms.transport.mcp import ToolsCallReply, ToolsCallResult
from agent_comms.transport.remote_core import ConnectorObserverClosed, ServiceFailed
from agent_comms.ui_model.observation import PeerClosed


@pytest.mark.parametrize('error,expected', [
    (MaintenanceHeldError('maintenance'), MaintenanceHeldError),
    (CoordinationReadUnavailable('busy source'), CoordinationReadUnavailable),
    (RuntimeError('broken service'), RelationViolationError),
])
def test_a_remote_failure_keeps_its_declared_recovery(error, expected):
    reply = ToolsCallReply(jsonrpc='2.0', id=1, result=ToolsCallResult.failed(error))
    with pytest.raises(expected, match=str(error)):
        reply.structured()


@pytest.mark.parametrize('ending', [PeerClosed(), ServiceFailed('original service traceback')])
def test_an_ended_observation_defers_reads_and_retains_its_report(ending):
    error = ConnectorObserverClosed(URL('https://core.example/observe'), ending)
    assert isinstance(error, CoordinationReadUnavailable)
    assert error.ending is ending
    assert ending.describe() in str(error)
