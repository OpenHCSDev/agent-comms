"""Named ordinary checks on a production-created owner/turn, plus real flock exclusion."""

from dataclasses import replace

import pytest

from agent_comms import agent_events as ae
from agent_comms import ordinary_admission_rules as rules
from agent_comms.reservation_rules import ReservationViolationError
from agent_comms.store_files import _store_lock
from agent_comms.thread_status import StoppedThreadStatus
from test_acp_goal_input_authority import owner


@pytest.mark.asyncio
async def test_ordinary_owner_checks_keep_distinct_fences_and_wire_exclusion(tmp_path, monkeypatch):
    agent, comms, _, _ = await owner(tmp_path, monkeypatch)
    inspected = []

    async def events(*args, **kwargs):
        admission = kwargs["send_boundary"]
        snapshot = comms.registry.snapshot()
        current = snapshot.threads["project"]
        check = rules.OrdinaryOwnerCheck(
            expected=admission.thread,
            current=current,
            registry=snapshot,
            canonical="project",
            admission=admission.admission,
            turn=admission.turn,
        )
        check.require_valid()

        def changed(thread):
            return replace(
                check, current=thread, registry=replace(snapshot, threads={"project": thread})
            )

        cases = {
            rules.OrdinaryIncarnationRule: changed(
                replace(current, created_at=current.created_at + 1)
            ),
            rules.OrdinaryRunningRule: replace(
                check, registry=replace(snapshot, statuses={"project": StoppedThreadStatus()})
            ),
            rules.OrdinaryAdmissionRule: replace(check, admission=check.admission + 1),
            rules.OrdinaryProcessRule: changed(
                replace(
                    current,
                    process_identity=replace(
                        current.process_identity, start_time=current.process_identity.start_time + 1
                    ),
                )
            ),
            rules.OrdinaryWorktreeRule: changed(
                replace(current, worktree=str(tmp_path / "different"))
            ),
            rules.OrdinaryTurnRule: changed(replace(current, active_turn=None)),
        }
        for declaration, invalid in cases.items():
            with pytest.raises(ReservationViolationError) as error:
                invalid.require_valid()
            assert type(error.value.rule) is declaration
            assert declaration.declared_name in str(error.value)
            inspected.append(declaration)
        # Ordinary turns require the same TurnId; unrelated lease metadata is not
        # private-stage authority and must not acquire its stronger equality fence.
        changed(
            replace(current, active_turn=replace(current.active_turn, started_at=1.0))
        ).require_valid()
        renamed = replace(current, name="renamed")
        replace(
            check,
            current=renamed,
            canonical="renamed",
            registry=replace(
                snapshot,
                threads={"renamed": renamed},
                aliases={"project": "renamed"},
                statuses={"renamed": snapshot.statuses["project"]},
                admission_generations={"renamed": admission.admission},
            ),
        ).require_valid()
        with admission(None, "a" * 32, args[2]) as allowed:
            assert allowed is True
            # A separately opened flock description cannot acquire while the native
            # callback is yielded, exactly where backend stdin.write executes.
            with (
                pytest.raises(BlockingIOError),
                _store_lock(comms._wire_lock_path, blocking=False),
            ):
                pytest.fail("wire exclusion released before native write")
        with _store_lock(comms._wire_lock_path, blocking=False):
            pass
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="Admission inspected")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn("project", "project", "Ordinary owner input")
        assert len(inspected) == 6
    finally:
        await agent.shutdown()
