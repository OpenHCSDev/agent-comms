"""Public type names may change; the existing ACP metadata must not."""

from agent_comms.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.session_load import (
    EnsuringSessionLoadPlan,
    ExistingSessionLoadPlan,
    FailedSessionLoadPlan,
    SessionLoadPlan,
)
from agent_comms.thread_identity import OwnerIdentity, RecordedThreadIncarnation
from agent_comms.thread_presentation import LiveThreadOwnerBinding


def test_default_load_metadata_keeps_its_original_shape():
    plan = EnsuringSessionLoadPlan()
    assert plan.metadata() == {"agentCommsLoad": {"kind": "ensuring"}}
    assert SessionLoadPlan.at_ingress(None) == plan
    assert SessionLoadPlan.at_ingress({"kind": "ensuring"}) == plan
    assert SessionLoadPlan.at_response(None) == plan


def test_witnessed_load_and_failure_metadata_keep_original_tags():
    binding = LiveThreadOwnerBinding(
        OwnerIdentity(RecordedThreadIncarnation("load-check", 123.0), 2),
        ProcessIdentity(1, 1),
    )
    plan = ExistingSessionLoadPlan(binding)
    payload = plan.metadata()
    assert payload == {
        "agentCommsLoad": {"kind": "existing", "binding": FieldCodec.encode(binding)}
    }
    assert SessionLoadPlan.at_ingress(payload["agentCommsLoad"]) == plan
    assert SessionLoadPlan.at_response(payload) == plan
    failed = FailedSessionLoadPlan(binding)
    error = failed.failure_metadata()
    assert error == {
        "agentCommsLoadFailure": {"kind": "failed", "binding": FieldCodec.encode(binding)}
    }
    assert FailedSessionLoadPlan.from_failure(error) == failed
