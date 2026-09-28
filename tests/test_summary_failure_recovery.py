"""Real native source/journal recovery preserves failed inputs and write uncertainty."""

import asyncio
import os
from pathlib import Path

import pytest

from agent_comms.backend import _session_revision
from agent_comms.field_codec import FieldCodec
from agent_comms.input_attempt import InputAttempt
from agent_comms.input_disposition import InputDispositions
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.owner_compaction_prepare import prepare_native_source
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.reservation_rules import ReservationViolationError
from agent_comms.selected_source import SelectedAdmissionSource
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import TurnId
from test_selected_owner_compaction_integration import owner_fixture


def test_finished_unbound_input_remains_visible_but_cannot_rebind(tmp_path):
    inputs = InputDispositions(tmp_path / "input_dispositions.json")
    inputs.record(
        "acp:failed", seq=None, owner="owner", admission=1, target="owner", text="keep me"
    )
    assert inputs.finish_unbound("acp:failed")
    row = inputs.read().rows["acp:failed"]
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
    assert not inputs.finish_unbound("acp:uncertain")
    assert inputs.read().rows["acp:uncertain"].declared_name == "bound_unknown"


@pytest.mark.skipif(
    not os.environ.get("PI_COMPACTION_TEST_PACKAGE"), reason="Actual native package required"
)
@pytest.mark.parametrize("bound", [False, True])
async def test_interrupted_summary_recovery_requires_unsent_original_and_unchanged_native(
    tmp_path, monkeypatch, bound
):
    async with owner_fixture(tmp_path, monkeypatch, real_host=True, goal=False) as (
        persistent,
        registry,
        inputs,
        session,
        launcher,
        info,
    ):
        owner, generation = registry.live_owner_with_generation("owner")
        package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
        bridge = await asyncio.to_thread(OwnerCompactionCommit, registry.store.path, package)
        prepared = await asyncio.to_thread(
            prepare_native_source,
            package,
            session,
            settings=PiCompactionSettings(1000, 10),
            context_window=info.context_size,
        )
        text = inputs.read().rows["acp:original"].source_text
        digest = TextDigest.of(text)
        operation = bridge.journal.reserve_selected_summary(
            session,
            {
                "source": FieldCodec.encode(
                    SelectedAdmissionSource(
                        owner=owner.process_identity,
                        incarnation=owner.incarnation,
                        turn=TurnId("earlier-failed-turn"),
                        ingress_key="acp:original",
                        admission_generation=owner.active_turn.admission_generation,
                        correction_witness="prior",
                        input_digest=digest,
                        original_digest=digest,
                        reserved_revision=_session_revision(session),
                    )
                ),
                "selected": dict(
                    provider="openai", modelId="gpt-4.1-mini", contextWindow=info.context_size
                ),
                "settings": dict(reserveTokens=1000, keepRecentTokens=10),
            },
        )
        bridge.journal.mark_selected_summary_unknown(operation)
        if bound:
            assert inputs.bind(
                "acp:original",
                admission=owner.active_turn.admission_generation,
                turn_id="earlier-failed-turn",
                native_id="a" * 32,
                text=text,
            )
        else:
            assert inputs.finish_unbound("acp:original")
        original = Path(session).read_bytes()
        dispositions = inputs.path.read_bytes()
        if bound:
            with pytest.raises(ReservationViolationError, match="native_binding_exists"):
                await asyncio.to_thread(
                    bridge.reconcile_interrupted_summaries, owner, generation, prepared.witness
                )
            assert bridge.journal.selected_summary(operation).state.declared_name == "unknown"
        else:
            await asyncio.to_thread(
                bridge.reconcile_interrupted_summaries, owner, generation, prepared.witness
            )
            state = bridge.journal.selected_summary(operation).state
            assert state.declared_name == "retired_unknown" and not state.original_eligible
            assert not bridge.journal.blocking_selected_summary(session)
        assert Path(session).read_bytes() == original
        assert inputs.path.read_bytes() == dispositions
