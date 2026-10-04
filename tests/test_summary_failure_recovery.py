"""Real native source/journal recovery preserves failed inputs and write uncertainty."""


import asyncio
import os
from pathlib import Path

import pytest

from agent_comms.input_attempt import InputAttempt
from agent_comms.input_disposition import InputDispositions
from agent_comms.compaction_records import SelectedSummarySource
from agent_comms.retained_task_facts import InputTaskFact, RetainedTaskFacts
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.owner_compaction_prepare import prepare_native_source
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.reservation_rules import ReservationViolationError
from agent_comms.selected_source import SelectedAdmissionSource, SessionRevision
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import TurnId
from test_selected_owner_compaction_integration import owner_fixture


def test_finished_unbound_input_remains_visible_but_cannot_rebind(tmp_path):
    inputs = InputDispositions(tmp_path / "input_dispositions.json")
    inputs.record(
        "acp:failed", seq=None, owner="owner", admission=1, target="owner", text="keep me"
    )
    row = inputs.settle_unbound(("acp:failed",)).rows["acp:failed"]
    assert row.declared_name == "not_sent" and row.unresolved
    assert row.public()["text"] == "keep me"
    assert not inputs.bind(
        "acp:failed", admission=1, turn_id="later", native_id="a" * 32, text="keep me"
    )
    assert "terminal" not in InputAttempt.names()
    inputs.record(
        "acp:uncertain", seq=None, owner="owner", admission=1, target="owner", text="bound"
    )
    assert inputs.bind(
        "acp:uncertain", admission=1, turn_id="earlier", native_id="b" * 32, text="bound"
    )
    assert inputs.settle_unbound(("acp:uncertain",)).rows["acp:uncertain"].declared_name == "bound_unknown"


@pytest.mark.skipif(
    not os.environ.get("PI_COMPACTION_TEST_PACKAGE"), reason="Actual native package required"
)
@pytest.mark.parametrize("bound", [False, True])
async def test_interrupted_summary_recovery_requires_unsent_original_and_unchanged_native(
    tmp_path, monkeypatch, bound
):
    async with owner_fixture(tmp_path, monkeypatch, goal=False) as (
        persistent,
        registry,
        inputs,
        session,
        launcher,
        info,
    ):
        owner, generation = registry.live_owner_with_generation("owner")
        selected = info.model.for_compaction(owner.model)
        package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
        bridge = await asyncio.to_thread(OwnerCompactionCommit, registry.store.path, package)
        prepared = (await asyncio.to_thread(
            prepare_native_source,
            package,
            session,
            settings=PiCompactionSettings(1000, 10),
            context_window=selected.context_window,
        )).require_ready()
        text = inputs.read().rows["acp:original"].source_text
        digest = TextDigest.of(text)
        operation = bridge.journal.summaries.reserve(
            session,
            SelectedSummarySource(
                source=SelectedAdmissionSource(
                    owner=owner.process_identity,
                    incarnation=owner.incarnation,
                    turn=TurnId("earlier-failed-turn"),
                    originals=inputs.read().original_provenances(("acp:original",)),
                    admission_generation=owner.active_turn.admission_generation,
                    correction_witness="prior",
                    input_digest=digest,
                    reserved_revision=SessionRevision.observe(session).require_available(),
                ),
                selected=selected,
                settings=PiCompactionSettings(1000, 10),
                retained=RetainedTaskFacts((InputTaskFact(inputs.read().rows["acp:original"]),)),
            ),
        )
        bridge.journal.summaries.mark_unknown(operation)
        if bound:
            assert inputs.bind(
                "acp:original",
                admission=owner.active_turn.admission_generation,
                turn_id="earlier-failed-turn",
                native_id="a" * 32,
                text=text,
            )
        else:
            assert inputs.settle_unbound(("acp:original",)).rows["acp:original"].public_status == "not_sent"
        original = Path(session).read_bytes()
        dispositions = inputs.path.read_bytes()
        if bound:
            with pytest.raises(ReservationViolationError, match="native_binding_exists"):
                await asyncio.to_thread(
                    bridge.reconcile_interrupted_summaries, owner, generation, prepared.witness
                )
            assert bridge.journal.summaries.get(operation).state.declared_name == "unknown"
        else:
            await asyncio.to_thread(
                bridge.reconcile_interrupted_summaries, owner, generation, prepared.witness
            )
            state = bridge.journal.summaries.get(operation).state
            assert state.declared_name == "retired_unknown" and not state.original_eligible
            assert not bridge.journal.summaries.blocking(session)
        assert Path(session).read_bytes() == original
        assert inputs.path.read_bytes() == dispositions


@pytest.mark.parametrize("state", ["reserved", "unknown", "refused"])
def test_summary_recovery_requires_original_no_write_source(tmp_path, state):
    """One transition family: wrong turn/binding/revision never retire the barrier."""
    from agent_comms.compaction_journal import CompactionJournal
    from agent_comms.compaction_errors import CompactionJournalError
    from agent_comms.compaction_records import CompactionOperation
    from agent_comms.compaction_states import IntentOperation
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.retained_task_facts import RetainedTaskFacts
    from agent_comms.selected_source import ManualSource
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.owner_compaction_settings import PiCompactionSettings
    from agent_comms.pi_summary_payloads import SelectedModel
    from dataclasses import replace

    session = tmp_path / "saved.jsonl"
    session.write_text('{"type":"session","version":3}\n')
    source = ManualSource(owner=ProcessIdentity.capture(os.getpid()),
        incarnation=ThreadIncarnation("owner", 1.), turn=TurnId("prior"),
        reserved_revision=SessionRevision.observe(str(session)).require_available())
    envelope = SelectedSummarySource(source, SelectedModel("fixture", "model", 1000),
        PiCompactionSettings(100, 100), RetainedTaskFacts(()))
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    operation = journal.summaries.reserve(str(session), envelope)
    if state == "unknown": journal.summaries.mark_unknown(operation)
    if state == "refused": journal.summaries.refuse(operation, "context_requires_compaction")
    attempt = journal.summaries.get(operation)
    inputs = InputDispositions(tmp_path / InputDispositions.filename)
    check = envelope.interrupted_check(source.reserved_revision, inputs.read(),
        source.incarnation, TurnId("new-authorized-turn"))
    with pytest.raises(ReservationViolationError, match="turn_still_active"):
        journal.summaries.retire_unchanged(attempt, replace(check, turn=source.turn))
    with pytest.raises(ReservationViolationError, match="owner_changed"):
        journal.summaries.retire_unchanged(attempt,
            replace(check, incarnation=ThreadIncarnation("owner", 2.)))
    with journal.transaction() as db:
        CompactionOperation("commit", str(session),
            '{"selectedSummaryOperationId":"' + operation + '"}',
            IntentOperation(), None).insert(db)
    with pytest.raises(CompactionJournalError, match="intent prevents"):
        journal.summaries.retire_unchanged(attempt, check)
    assert journal.summaries.get(operation) == attempt
    session.write_text(session.read_text() + '{}\n')
    with pytest.raises(ReservationViolationError, match="session_changed"):
        journal.summaries.retire_unchanged(attempt, replace(check,
            revision=SessionRevision.observe(str(session))))
    assert journal.summaries.get(operation) == attempt


def test_known_refusal_retirement_does_not_admit_or_reclassify_original(tmp_path):
    from agent_comms.compaction_journal import CompactionJournal
    from agent_comms.compaction_states import RetiredRefusalSummary
    from agent_comms.pi_summary_payloads import SelectedModel
    from agent_comms.input_attempt import NotSentInput
    from agent_comms.threads import Thread
    from agent_comms.child_process import ProcessIdentity

    session = tmp_path / "saved.jsonl"
    session.write_text('{"type":"session","version":3}\n')
    inputs = InputDispositions(tmp_path / InputDispositions.filename)
    inputs.record("acp:prior", seq=None, owner="owner", admission=1,
        target="owner", text="original never replayed")
    rows = inputs.read()
    source = SelectedAdmissionSource.capture(
        Thread(
            'owner', frozenset(), str(tmp_path), created_at=1.,
            process_identity=ProcessIdentity.capture(os.getpid())),
        TurnId("prior"), 1, ("acp:prior",), rows, "original never replayed",
        SessionRevision.observe(str(session)).require_available())
    envelope = SelectedSummarySource(source, SelectedModel("fixture", "model", 1000),
        PiCompactionSettings(100, 100), RetainedTaskFacts((InputTaskFact(rows.lookup("acp:prior")),)))
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    op = journal.summaries.reserve(str(session), envelope)
    journal.summaries.refuse(op, "context_requires_compaction")
    inputs.settle_unbound(("acp:prior",))
    before = inputs.path.read_bytes()
    check = envelope.interrupted_check(source.reserved_revision, inputs.read(),
        source.incarnation, TurnId("distinct-authorized-input"))
    journal.summaries.retire_unchanged(journal.summaries.get(op), check)
    assert isinstance(journal.summaries.get(op).state, RetiredRefusalSummary)
    assert not journal.summaries.blocking(str(session))
    assert isinstance(inputs.read().lookup("acp:prior"), NotSentInput)
    assert inputs.path.read_bytes() == before
    assert not inputs.bind("acp:prior", admission=1, turn_id="distinct-authorized-input",
        native_id="a" * 32, text="original never replayed")
