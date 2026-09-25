"""The live owner separates queued delivery from earlier unresolved notices."""

import os

import pytest

from delivery_owner_fixture import queued_delivery_owner


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_owner_queue_projection_clear_preserves_pending_and_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv("PI_AGENT_ID", raising=False)
    monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    async with queued_delivery_owner(tmp_path) as (owner, proxy, session, incoming):
        ledger = owner._dispositions
        before = ledger._read()
        cursor_before = owner._delivery_cursors.path.read_bytes()
        snapshot = await proxy.request("input_dispositions")
        assert snapshot["currentScope"] == "owner_queue"
        assert [row["inputId"] for row in snapshot["inputs"]] == [f"bus:{incoming.seq}"]
        assert snapshot["historicalCount"] == 2 and snapshot["historicalInputs"] == []
        # Without live owner context, the compatibility projection cannot claim
        # that current-looking inputs are earlier or eligible for notice dismissal.
        assert len(owner._comms.input_delivery(session)["inputs"]) == 3
        cleared = await proxy.request("dismiss_historical_inputs")
        assert cleared["inputs"] == snapshot["inputs"]
        assert cleared["historicalCount"] == 0 and cleared["dismissedHistoricalCount"] == 2
        details = await proxy.request("input_dispositions", include_history=True)
        assert len(details["historicalInputs"]) == 2
        assert all(row["noticeDismissed"] for row in details["historicalInputs"])
        assert not ledger.get(f"bus:{incoming.seq}").get("notice_dismissed")
        after = ledger._read()
        assert {
            key: {k: v for k, v in row.items() if k != "notice_dismissed"}
            for key, row in after.items()
        } == before
        assert owner._delivery_cursors.path.read_bytes() == cursor_before
        assert len(owner._pending_turns[session]) == 1 and not owner._backend_inboxes
        # A new input arriving after the clear cannot inherit a cleared notice.
        second = owner._comms.send_message("peer", session, "A second new input")
        await owner._drain_inbox(session)
        fresh = await proxy.request("input_dispositions")
        assert [r["sequence"] for r in fresh["inputs"]] == [incoming.seq, second.seq]
        assert fresh["dismissedHistoricalCount"] == 2
