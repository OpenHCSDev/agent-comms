"""Actual selected native summary and authority writer, without a user prompt."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDocument
from agent_comms.owner_compaction_manual import compact_manual_owner
from selected_summary_cases import admission_identity, manual_source
from test_selected_owner_compaction_integration import owner_fixture

pytestmark = pytest.mark.skipif(
    not os.environ.get("PI_COMPACTION_TEST_PACKAGE"), reason="Prepared native fixture required"
)


async def test_explicit_manual_selected_commit_never_invents_original_input(tmp_path, monkeypatch):
    async with owner_fixture(tmp_path, monkeypatch, real_host=True, goal=False) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        # The ordinary fixture seeds an original input. This explicit command has none.
        inputs.replace(
            InputDocument(
                rows={key: row for key, row in inputs.read().rows.items() if key != "acp:original"}
            )
        )
        comms = Comms(tmp_path)
        runner = SimpleNamespace(
            persistent_backends={"owner": persistent},
            comms=comms,
            agent_bin=launcher,
            effects=SimpleNamespace(
                _private_nk_native_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]).resolve()
            ),
        )
        before_inputs = inputs.path.read_bytes()
        result = await compact_manual_owner(runner, "owner", "owner", info, None)
        assert result["ok"] is True
        journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
        assert journal.get(result["commitId"]).state.committed
        (attempt,) = journal.selected_summaries(file)
        assert isinstance(attempt.state, ManualCommittedSummary)
        assert not attempt.state.original_eligible
        assert native_input_admitted(tmp_path, file)
        assert inputs.path.read_bytes() == before_inputs
        rows = [json.loads(line) for line in Path(file).read_text().splitlines()]
        assert sum(row["type"] == "compaction" for row in rows) == 1
        assert persistent.proc is None and persistent.reopen_required == file


async def test_explicit_manual_recovers_known_refusal_without_replaying_unknown(
    tmp_path, monkeypatch
):
    async with owner_fixture(tmp_path, monkeypatch, real_host=True, goal=False) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        owner = registry.require("owner")
        journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
        operation = journal.reserve_selected_summary(
            file,
            {
                "source": FieldCodec.encode(
                    admission_identity(
                        file,
                        text=inputs.read().rows["acp:original"].source_text,
                        key="acp:original",
                        turn=owner.active_turn.id,
                        owner=owner.name,
                        incarnation=owner.incarnation,
                        admission=owner.active_turn.admission_generation,
                    ).source
                ),
                "selected": {
                    "provider": "openai",
                    "modelId": "gpt-4.1-mini",
                    "contextWindow": info.context_size,
                },
                "settings": {"reserveTokens": 1000, "keepRecentTokens": 10},
            },
        )
        journal.refuse_selected_summary(operation, "limit_exceeded")
        before_inputs = inputs.path.read_bytes()
        runner = SimpleNamespace(
            persistent_backends={"owner": persistent},
            comms=Comms(tmp_path),
            agent_bin=launcher,
            effects=SimpleNamespace(
                _private_nk_native_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]).resolve()
            ),
        )
        result = await compact_manual_owner(runner, "owner", "owner", info, None)
        assert result["ok"] is True
        assert journal.selected_summary(operation).state.declared_name == "retired_refusal"
        assert inputs.path.read_bytes() == before_inputs
        assert inputs.read().rows["acp:original"].unattempted
        assert native_input_admitted(tmp_path, file)
        rows = [json.loads(line) for line in Path(file).read_text().splitlines()]
        assert sum(row["type"] == "compaction" for row in rows) == 1
        assert (
            sum(row["type"] == "message" and row["message"]["role"] == "user" for row in rows) == 5
        )


@pytest.mark.parametrize("uncertain", [False, True], ids=["reserved", "unknown"])
async def test_manual_does_not_retire_or_repeat_uncertain_provider(
    tmp_path, monkeypatch, uncertain
):
    from agent_comms.compaction_journal import CompactionJournalError

    async with owner_fixture(tmp_path, monkeypatch, real_host=True, goal=False) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        inputs.replace(
            InputDocument(
                rows={key: row for key, row in inputs.read().rows.items() if key != "acp:original"}
            )
        )
        journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
        operation = journal.reserve_selected_summary(
            file,
            {
                "source": manual_source(file, incarnation=registry.require("owner").incarnation),
                "selected": {
                    "provider": "openai",
                    "modelId": "gpt-4.1-mini",
                    "contextWindow": info.context_size,
                },
                "settings": {"reserveTokens": 1000, "keepRecentTokens": 10},
            },
        )
        if uncertain:
            journal.mark_selected_summary_unknown(operation)
        attempt = journal.selected_summary(operation)
        before = Path(file).read_bytes()
        runner = SimpleNamespace(
            persistent_backends={"owner": persistent},
            comms=Comms(tmp_path),
            agent_bin=launcher,
            effects=SimpleNamespace(
                _private_nk_native_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]).resolve()
            ),
        )
        with pytest.raises(CompactionJournalError, match="uncertain"):
            await compact_manual_owner(runner, "owner", "owner", info, None)
        assert journal.selected_summary(operation) == attempt
        assert Path(file).read_bytes() == before
        assert not native_input_admitted(tmp_path, file)
