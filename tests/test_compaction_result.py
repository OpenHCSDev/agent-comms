"""The current internal socket reply decodes once into declaration-owned effects."""

from dataclasses import dataclass

import pytest
from acp import RequestError

from agent_comms.acp_extension import CompactionCommittedUpdate, decode_updates
from agent_comms.agent_events import ManualCompactionEnd
from agent_comms.compaction_result import (
    CommittedCompactionResult,
    CompactionResult,
    RefusedCompactionResult,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_settings import PiSettingsEvidenceError


@pytest.mark.parametrize(
    "result",
    [
        CommittedCompactionResult("retained summary", "actual-commit"),
        RefusedCompactionResult("provider disconnected; no replay"),
    ],
)
def test_typed_result_roundtrip(result):
    assert FieldCodec.decode(CompactionResult, FieldCodec.encode(result)) == result


def test_result_owns_terminal_and_acp_semantics():
    result = CommittedCompactionResult("summary", "commit")
    assert result.terminal_event() == ManualCompactionEnd(aborted=False, summary="summary")
    assert decode_updates(result.prompt_response().field_meta) == (
        CompactionCommittedUpdate("commit", "summary"),
    )
    refused = RefusedCompactionResult("actual provider reason")
    assert refused.terminal_event() == ManualCompactionEnd(aborted=True, summary=refused.error)
    with pytest.raises(RequestError) as error:
        refused.prompt_response()
    assert error.value.data == {"reason": refused.error}


def test_new_member_owns_wire_and_behavior_without_dispatch_edits(monkeypatch):
    monkeypatch.setattr(CompactionResult, "__registry__", dict(CompactionResult.__registry__))

    @dataclass(frozen=True)
    class ObservedCompactionResult(RefusedCompactionResult):
        pass

    outcome = ObservedCompactionResult("observation")
    assert FieldCodec.decode(CompactionResult, FieldCodec.encode(outcome)) == outcome
    assert outcome.terminal_event().summary == "observation"
    assert not outcome.adaptive_result()
    with pytest.raises(PiSettingsEvidenceError, match="observation"):
        outcome.require_prepared()
    with pytest.raises(RequestError) as error:
        outcome.prompt_response()
    assert error.value.data == {"reason": outcome.error}
