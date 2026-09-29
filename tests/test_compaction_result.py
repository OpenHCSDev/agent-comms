"""The existing socket shape decodes once into declaration-owned effects."""

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


@pytest.mark.parametrize(
    "result,wire",
    [
        (
            CommittedCompactionResult("retained summary", "actual-commit"),
            {"ok": True, "summary": "retained summary", "commitId": "actual-commit"},
        ),
        (
            RefusedCompactionResult("provider disconnected; no replay"),
            {"ok": False, "error": "provider disconnected; no replay"},
        ),
    ],
)
def test_exact_external_shape_and_typed_roundtrip(result, wire):
    assert FieldCodec.encode(result) == wire
    assert FieldCodec.decode(CompactionResult, wire) == result


@pytest.mark.parametrize("tag", [0, 1, None, "true", "committed"])
def test_boolean_discriminator_is_not_coerced(tag):
    with pytest.raises(ValueError):
        FieldCodec.decode(CompactionResult, {"ok": tag, "error": "refused"})


@pytest.mark.parametrize(
    "wire",
    [
        {"ok": True, "summary": "summary"},
        {"ok": False},
        {"ok": False, "error": 42},
        {"ok": True, "summary": "summary", "commitId": "commit", "error": "extra"},
    ],
)
def test_wrong_case_fields_fail_at_boundary(wire):
    with pytest.raises((ValueError, TypeError)):
        FieldCodec.decode(CompactionResult, wire)


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
    class ObservedCompactionResult(CompactionResult):
        detail: str

        @classmethod
        def wire_tag(cls):
            return "observed-test-case"

        def terminal_event(self):
            return ManualCompactionEnd(aborted=True, summary=self.detail)

        def prompt_response(self):
            raise RequestError(-32603, self.detail, {"reason": self.detail})

    outcome = ObservedCompactionResult("observation")
    assert FieldCodec.decode(CompactionResult, FieldCodec.encode(outcome)) == outcome
    assert outcome.terminal_event().summary == "observation"
