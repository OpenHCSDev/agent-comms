"""Named private claim_scope refusals against production-created SQLite rows."""
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from binding_reads import binding_rows

import os
from dataclasses import replace

import pytest

from agent_comms.turn_lease import NoTurn

import agent_comms.private_send_rules as rules
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_input_record import TriageNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordinator import Coordination
from agent_comms.goals import Goal

from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_claim_epoch import RecordedNativeClaim
from agent_comms.private_sidecar import native_request_digest
from agent_comms.reservation_rules import ReservationRule, ReservationViolationError
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import UserThreadRole
from agent_comms.pi_vocabulary import ThinkingLevel
from test_coordinated_runtime import _root
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


@pytest.mark.parametrize("direct", [False, True])
async def test_durable_private_claim_names_each_changed_authority(tmp_path, monkeypatch, direct, *, native_runtime_package, cursor_delivery):
    root, root_id, comms, _, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    observed = []

    class InspectedError(Exception):
        pass

    async def inspect_before_native(*args, **kwargs):
        claim_scope = kwargs["prompt_send_boundary"]
        with Coordination(root / "coordination.sqlite3") as store:
            row = NativeRuntimeInput.one(store.session._connection, input_id=claim_scope.input_id)
            binding = binding_rows(store, (claim_scope.input_id,)).get(claim_scope.input_id)
        stage = claim_scope.stage
        expected = claim_scope.participant.coordinator_identity(stage.recipient_lookup)
        reservation = rules.NativeReservationCheck(
            row=row, stage=stage, owner=expected, token_digest=claim_scope.token_digest
        )
        bound = rules.NativeBindingCheck(
            row=binding,
            stage=stage,
            owner=expected,
            wire_root_id=root_id,
            prompt_digest=TextDigest(native_request_digest(claim_scope.prompt.text)),
        )
        current, generation = comms.registry.live_owner_with_claim(owner.name)
        registry = rules.GoalRegistryClaimCheck(
            actual=current,
            expected=current,
            claim_scope=generation,
            expected_claim=generation,
            process=ProcessIdentity.capture(os.getpid()),
        )
        selected_claim, = stage.assignments
        claim = rules.NativeClaimCheck(captured=selected_claim, current=selected_claim)
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
            rules.RegistryClaimRule: replace(registry, claim_scope=generation + 1),
            rules.RegistryRoleRule: replace(
                registry, actual=replace(current, role=UserThreadRole)
            ),
            rules.RegistryWorktreeRule: replace(
                registry, actual=replace(current, worktree=str(tmp_path / "other"))
            ),
            rules.RegistryModelRule: replace(registry, actual=replace(current, model="fixture/changed")),
            rules.RegistryThinkingRule: replace(registry, actual=replace(current, thinking_level=next(level for level in ThinkingLevel.members_with(ThinkingLevel) if level is not current.thinking_level))),
            rules.RegistrySessionRule: replace(registry, actual=replace(current, session_file=str(tmp_path / "changed.jsonl"))),
            rules.RegistryTurnRule: replace(registry, actual=replace(current, active_turn=NoTurn())),
            rules.RegistryGoalRule: replace(
                registry, actual=replace(current, goal=Goal("changed", "changed", published_seq=0))
            ),
            rules.NativeInputIdentityRule: replace(
                reservation, row=replace(row, owner_generation=expected.generation + 1)
            ),
            rules.NativeTokenRule: replace(reservation, token_digest="0" * 64),
            rules.NativeAlreadyBoundRule: replace(
                reservation, row=replace(row, sent_owner_claimed_generation=RecordedNativeClaim(1))
            ),
            rules.NativeAlreadyProvenRule: replace(
                reservation, row=replace(row, session_id="returned")
            ),
            rules.NativeAlreadyDecidedRule: replace(
                reservation, row=replace(row, verdict=IgnoreSelectedTriage)
            ),
            rules.NativeBindingRootRule: replace(bound, wire_root_id="0" * 32),
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
            root=root,
            wire_root_id=root_id,
            owner_name=owner.name,
            native_package=native_runtime_package,
            cursor_delivery=cursor_delivery,
        ).run()
    assert observed == [True]
