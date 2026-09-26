"""Selected summary seam is fail-closed; local fake RPC never carries user input."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_comms.selected_pi_child_deadline import SelectedChildUnknown
from agent_comms.selected_pi_summary_rpc import SelectedSummarySlot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespace fake")

FAKE = r"""
import json,sys,time
row=json.loads(sys.stdin.readline())
if sys.argv[1]=='hang':
    while True: time.sleep(1)
if sys.argv[1]=='wrong': row['incarnation']='wrong-identity'
print(json.dumps({'type':'selected_fake_response','owner':row['owner'],
                  'session':row['session'],'operation':row['operation'],
                  'incarnation':row['incarnation'],'ok':True}),flush=True)
while True: time.sleep(1)
"""


@pytest.mark.asyncio
async def test_dedicated_fake_rpc_exact_echo_and_no_original_input(tmp_path: Path):
    slot = SelectedSummarySlot("owner-epoch", "session-id")
    response = await slot.exchange_fake_rpc(
        operation="compaction-123",
        command=(sys.executable, "-u", "-c", FAKE, "good"),
        receipt=tmp_path / "one",
    )
    assert response["operation"] == "compaction-123"
    assert response["incarnation"] == json.loads((tmp_path / "one").read_text())["token"]
    assert json.loads((tmp_path / "one").read_text())["status"] == "unknown"
    with pytest.raises(SelectedChildUnknown, match="disabled"):
        await slot.run_selected_summary(provider="paid", model="selected")


@pytest.mark.asyncio
async def test_wrong_incarnation_and_uncooperative_fake_remain_unknown(tmp_path: Path):
    slot = SelectedSummarySlot("owner-epoch", "session-id")
    with pytest.raises(SelectedChildUnknown, match="Unbound"):
        await slot.exchange_fake_rpc(
            operation="op1",
            command=(sys.executable, "-u", "-c", FAKE, "wrong"),
            receipt=tmp_path / "wrong",
        )
    assert json.loads((tmp_path / "wrong").read_text())["status"] == "unknown"
    with pytest.raises(SelectedChildUnknown):
        await slot.exchange_fake_rpc(
            operation="op2",
            command=(sys.executable, "-u", "-c", FAKE, "hang"),
            receipt=tmp_path / "hang",
            timeout_seconds=1,
        )
    assert json.loads((tmp_path / "hang").read_text())["status"] == "unknown"


@pytest.mark.asyncio
async def test_cancellation_retains_slot_until_exact_child_is_retired(tmp_path: Path):
    slot = SelectedSummarySlot("owner-epoch", "session-id")
    caller = asyncio.create_task(
        slot.exchange_fake_rpc(
            operation="hang",
            command=(sys.executable, "-u", "-c", FAKE, "hang"),
            receipt=tmp_path / "cancel",
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0.1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    with pytest.raises(SelectedChildUnknown, match="still retiring"):
        await slot.exchange_fake_rpc(
            operation="never",
            command=(sys.executable, "-u", "-c", FAKE, "good"),
            receipt=tmp_path / "never",
        )
    await asyncio.sleep(1.25)
    assert slot._attempt is not None and slot._attempt.done()
    assert not (tmp_path / "never").exists()
    assert json.loads((tmp_path / "cancel").read_text())["status"] == "unknown"
