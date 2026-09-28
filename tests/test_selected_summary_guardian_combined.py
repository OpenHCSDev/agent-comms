"""Default-OFF Python journal + exact dedicated-child fake RPC; no Pi/provider."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.compaction_journal import CompactionJournal, CompactionJournalError
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.selected_pi_child_deadline import SelectedChildUnknown
from agent_comms.selected_pi_summary_rpc import SelectedSummarySlot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespace fake only")

FAKE = r"""
import json,sys,time
request=json.loads(sys.stdin.readline())
if sys.argv[1]=='hang':
    while True: time.sleep(1)
print(json.dumps({'type':'selected_fake_response','owner':request['owner'],
    'session':request['session'],'operation':request['operation'],
    'incarnation':request['incarnation'],'ok':True}),flush=True)
while True: time.sleep(1)
"""


def _reserved(tmp_path: Path):
    comms = wire(tmp_path / "wire")
    session = tmp_path / "saved.jsonl"
    session.write_text("{}\n")
    journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
    operation_id = "a" * 32
    source = {
        "source": {"witnessRevision": "fake:unchanged", "turnId": "fake-turn"},
        "selected": {"provider": "fake", "modelId": "fake"},
        "settings": {"reserveTokens": 100},
    }
    assert (
        journal.reserve_selected_summary(str(session), source, operation_id=operation_id)
        == operation_id
    )
    assert not native_input_admitted(comms.root, str(session))
    return comms, session, journal, operation_id, SelectedSummarySlot("fake-owner", str(session))


@pytest.mark.asyncio
async def test_exact_reserved_operation_fake_rpc_never_grants_original_input(tmp_path: Path):
    comms, session, journal, operation_id, slot = _reserved(tmp_path)
    receipt = tmp_path / "fake-receipt"
    result = await slot.exchange_fake_rpc(
        operation=operation_id,
        command=(sys.executable, "-u", "-c", FAKE, "ok"),
        receipt=receipt,
    )
    assert result["operation"] == operation_id
    assert result["session"] == str(session)
    assert json.loads(receipt.read_text())["status"] == "unknown"
    assert slot._attempt is not None and slot._attempt.done()
    assert journal.selected_summary(operation_id).state.declared_name == "reserved"
    assert not native_input_admitted(comms.root, str(session))
    journal.mark_selected_summary_unknown(operation_id)
    assert (
        CompactionJournal(journal.path).selected_summary(operation_id).state.declared_name
        == "unknown"
    )
    assert not native_input_admitted(comms.root, str(session))
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.reserve_selected_summary(
            str(session), {"source": {"w": "x"}, "selected": {"p": "x"}, "settings": {"s": "x"}}
        )
    with pytest.raises(TypeError, match="provider"):
        await slot.run_selected_summary(provider="paid", model="selected")


@pytest.mark.asyncio
async def test_fake_timeout_retirement_keeps_original_and_new_operations_blocked(tmp_path: Path):
    comms, session, journal, operation_id, slot = _reserved(tmp_path)
    receipt = tmp_path / "timeout-receipt"
    with pytest.raises(SelectedChildUnknown):
        await slot.exchange_fake_rpc(
            operation=operation_id,
            command=(sys.executable, "-u", "-c", FAKE, "hang"),
            receipt=receipt,
            timeout_seconds=1,
        )
    assert json.loads(receipt.read_text())["status"] == "unknown"
    assert slot._attempt is not None and slot._attempt.done()
    journal.mark_selected_summary_unknown(operation_id)
    assert not native_input_admitted(comms.root, str(session))
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.reserve_selected_summary(
            str(session),
            {"source": {"w": "x"}, "selected": {"p": "x"}, "settings": {"s": "x"}},
        )
