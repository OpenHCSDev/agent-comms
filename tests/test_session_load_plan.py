"""Original ACP load witnesses retain their wire contract and owner semantics."""

import pytest

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.thread_identity import (
    OwnerIdentity,
    RecordedThreadIncarnation,
)
from agent_comms.registry.thread_presentation import (
    EnsuringSessionLoadPlan,
    ExistingSessionLoadPlan,
    FailedSessionLoadPlan,
    LiveThreadOwnerBinding,
    SessionLoadPlan,
    UnavailableThreadOwnerBinding,
)


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
    assert SessionLoadPlan.from_failure(error) == failed


@pytest.mark.parametrize("plan_type", (ExistingSessionLoadPlan, FailedSessionLoadPlan))
async def test_only_a_superseding_live_owner_supplies_an_existing_load(plan_type):
    original = LiveThreadOwnerBinding(
        OwnerIdentity(RecordedThreadIncarnation("original", 123.0), 2),
        ProcessIdentity(1, 1),
    )
    previous = plan_type(original)
    loads: list[SessionLoadPlan] = []

    async def reload(plan: SessionLoadPlan) -> None:
        loads.append(plan)

    await UnavailableThreadOwnerBinding().reload_if_superseded(previous, reload)
    await original.reload_if_superseded(previous, reload)
    await LiveThreadOwnerBinding(
        OwnerIdentity(original.owner.incarnation, 1), ProcessIdentity(2, 2),
    ).reload_if_superseded(previous, reload)
    await LiveThreadOwnerBinding(
        OwnerIdentity(RecordedThreadIncarnation("replacement", 124.0), 3),
        ProcessIdentity(2, 2),
    ).reload_if_superseded(previous, reload)
    assert not loads
    renamed = original.owner.incarnation.renamed("renamed")
    successor = LiveThreadOwnerBinding(OwnerIdentity(renamed, 3), ProcessIdentity(2, 2))
    await successor.reload_if_superseded(previous, reload)
    assert loads == [ExistingSessionLoadPlan(successor)]
    assert previous.binding == original


@pytest.mark.parametrize("metadata", (None, {}))
async def test_an_ensuring_load_is_not_an_old_owner_witness(metadata):
    binding = LiveThreadOwnerBinding(
        OwnerIdentity(RecordedThreadIncarnation("source", 123.0), 2),
        ProcessIdentity(1, 1),
    )
    loads: list[SessionLoadPlan] = []

    async def reload(plan: SessionLoadPlan) -> None:
        loads.append(plan)

    plan = SessionLoadPlan.from_failure(metadata)
    assert plan == EnsuringSessionLoadPlan()
    await binding.reload_if_superseded(plan, reload)
    assert not loads
