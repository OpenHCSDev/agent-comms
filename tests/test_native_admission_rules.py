"""Named private admission refusals against production-created SQLite rows."""

import os
from dataclasses import replace

import pytest

from agent_comms import native_admission_rules as rules
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_input_record import TriageNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordinator import Coordination
from agent_comms.goals import Goal
from agent_comms.native_prompt_binding import read_expected_prompt_binding
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_admission_epoch import RecordedNativeAdmission
from agent_comms.private_sidecar import native_request_digest
from agent_comms.reservation_rules import ReservationRule, ReservationViolationError
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadRole
from agent_comms.pi_vocabulary import ThinkingLevel
from test_coordinated_runtime import _root
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


@pytest.mark.parametrize("direct", [False, True])
async def test_durable_private_admission_names_each_changed_authority(
    tmp_path, monkeypatch, direct
):
    root, root_id, comms, _, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    observed = []

    class InspectedError(Exception):
        pass

    async def inspect_before_native(*args, **kwargs):
        admission = kwargs["prompt_send_boundary"]
        with Coordination(root / "coordination.sqlite3") as store:
            row = NativeRuntimeInput.one(store.session._connection, input_id=admission.input_id)
            binding = read_expected_prompt_binding(store, admission.input_id)
        stage = admission.stage
        expected = admission.participant.coordinator_identity(stage.assignment.recipient_lookup)
        reservation = rules.NativeReservationCheck(
            row=row, stage=stage, owner=expected, token_digest=admission.token_digest
        )
        bound = rules.NativeBindingCheck(
            row=binding,
            stage=stage,
            owner=expected,
            wire_root_id=root_id,
            prompt_digest=TextDigest(native_request_digest(admission.prompt)),
        )
        current, generation = comms.registry.live_owner_with_admission(owner.name)
        registry = rules.GoalRegistryAdmissionCheck(
            actual=current,
            expected=current,
            admission=generation,
            expected_admission=generation,
            process=ProcessIdentity.capture(os.getpid()),
        )
        claim = rules.NativeClaimCheck(captured=stage.assignment, current=stage.assignment)
        for check in (registry, reservation, bound, claim):
            check.require_valid()
        cases = {
            rules.RegistryIncarnationRule: replace(
                registry, actual=replace(current, created_at=current.created_at + 1)
            ),
            rules.RegistryProcessRule: replace(
                registry,
                process=replace(registry.process, start_time=registry.process.start_time + 1),
            ),
            rules.RegistryAdmissionRule: replace(registry, admission=generation + 1),
            rules.RegistryRoleRule: replace(
                registry, actual=replace(current, role=ThreadRole.USER)
            ),
            rules.RegistryWorktreeRule: replace(
                registry, actual=replace(current, worktree=str(tmp_path / "other"))
            ),
            rules.RegistryModelRule: replace(registry, actual=replace(current, model="fixture/changed")),
            rules.RegistryThinkingRule: replace(registry, actual=replace(current, thinking_level=next(level for level in ThinkingLevel.members_with(ThinkingLevel) if level is not current.thinking_level))),
            rules.RegistrySessionRule: replace(registry, actual=replace(current, session_file=str(tmp_path / "changed.jsonl"))),
            rules.RegistryTurnRule: replace(registry, actual=replace(current, active_turn=None)),
            rules.RegistryGoalRule: replace(
                registry, actual=replace(current, goal=Goal("changed", "changed"))
            ),
            rules.NativeInputIdentityRule: replace(
                reservation, row=replace(row, owner_generation=expected.generation + 1)
            ),
            rules.NativeTokenRule: replace(reservation, token_digest="0" * 64),
            rules.NativeAlreadyAdmittedRule: replace(
                reservation, row=replace(row, sent_owner_admission_generation=RecordedNativeAdmission(1))
            ),
            rules.NativeAlreadyProvenRule: replace(
                reservation, row=replace(row, session_id="returned")
            ),
            rules.NativeAlreadyDecidedRule: replace(
                reservation, row=replace(row, verdict=IgnoreSelectedTriage)
            ),
            rules.NativeBindingRootRule: replace(bound, wire_root_id="0" * 32),
            rules.NativeBindingSourceRule: replace(
                bound, row=replace(binding, source_seq=binding.source_seq + 1)
            ),
            rules.NativeBindingContentRule: replace(bound, prompt_digest=TextDigest.of("changed")),
            rules.NativeClaimRecipientRule: replace(
                claim, current=replace(claim.current, recipient="another")
            ),
            rules.NativeClaimLookupRule: replace(
                claim, current=replace(claim.current, recipient_lookup="another")
            ),
            rules.NativeClaimSequenceRule: replace(
                claim, current=replace(claim.current, wire_seq=claim.current.wire_seq + 1)
            ),
            rules.NativeClaimMessageRule: replace(
                claim, current=replace(claim.current, message_id="another")
            ),
        }
        # Wake-mode variants belong to the existing assignment-state family.
        from agent_comms.assignment_states import FullPendingAssignment, TriagePendingAssignment

        cases[rules.NativeClaimModeRule] = replace(
            claim,
            current=replace(
                claim.current,
                lifecycle=TriagePendingAssignment() if direct else FullPendingAssignment(),
            ),
        )
        applicable = {
            declaration
            for declaration in ReservationRule.members_with(ReservationRule)
            if any(
                isinstance(check, declaration.check_type)
                for check in (registry, reservation, bound, claim)
            )
        }
        assert cases.keys() == applicable
        for declaration, check in cases.items():
            with pytest.raises(ReservationViolationError) as caught:
                check.require_valid()
            assert type(caught.value.rule) is declaration
            assert declaration.declared_name in str(caught.value)
        # The shared owner/attempt rules also apply to the independent prelaunch row.
        for changes, refusal in (
            ({"owner_generation": expected.generation + 1}, rules.NativeInputIdentityRule),
        ):
            with pytest.raises(ReservationViolationError) as caught:
                replace(bound, row=replace(binding, **changes)).require_valid()
            assert type(caught.value.rule) is refusal
        try:
            changed_attempt = replace(binding, attempt_ordinal=99)
        except IdentityConflict:
            # Triage has no execution: reject partial original SQL at acquisition.
            assert isinstance(binding.execution, TriageNativeExecution)
        else:
            with pytest.raises(ReservationViolationError) as caught:
                replace(bound, row=changed_attempt).require_valid()
            assert type(caught.value.rule) is rules.NativeInputIdentityRule
        observed.append(True)
        raise InspectedError

    monkeypatch.setattr(
        "agent_comms.tracked_turn.TrackedTurnSession.execute", inspect_before_native
    )
    with pytest.raises(InspectedError):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name=owner.name, native_package=tmp_path
        ).run()
    assert observed == [True]
