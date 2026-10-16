"""Unstarted native input failure preserves originals across distinct explicit inputs."""

from __future__ import annotations

import json

import pytest
from acp import RequestError
from acp.schema import TextContentBlock

from agent_comms import backend
from agent_comms.acp_extension import InputFailedUpdate, decode_updates
from agent_comms.acp_failure import RequestFailure
from agent_comms.native_pi import NativePiUnavailable
from test_backend_native_lifecycle import native_backend as native_backend
from stored_inputs import stored_inputs


async def test_unstarted_input_failure_preserves_original_across_explicit_successor(
    native_backend, monkeypatch,
):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    updates = []
    detail = "Pi preflight ended before attestation"

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    async with native.open_owner(client=Client()) as (agent, session_id):
        async def refused_preflight(self):
            raise NativePiUnavailable(detail)

        # Keep the acquired native child and the real input/turn/error owners;
        # refuse only their next input preflight, before any raw prompt write.
        monkeypatch.setattr(backend.TurnSession, "input_ready", refused_preflight)
        preserved = None
        for text in ("lost prompt", "distinct explicit input"):
            before = set((native.root / "diagnostics").glob("*.json"))
            updates.clear()
            with pytest.raises(RequestError) as caught:
                await agent.prompt(session_id, [TextContentBlock(type="text", text=text)])
            failure = RequestFailure.from_error(
                caught.value.code, str(caught.value), caught.value.data,
            )
            assert isinstance(caught.value.__cause__, NativePiUnavailable)
            assert failure.notification_published
            assert failure.failure.input_state.public_status == "not_sent"
            failed = tuple(
                fact for update in updates for fact in decode_updates(update.field_meta)
                if isinstance(fact, InputFailedUpdate)
            )
            assert len(failed) == 1 and failed[0].text == text
            diagnostic, = set((native.root / "diagnostics").glob("*.json")) - before
            assert detail in json.loads(diagnostic.read_text())["source_error"]
            assert failed[0].failure == failure.failure
            assert failed[0].failure.description == (
                f"Native turn failed. [Open diagnostic]({diagnostic.as_uri()})"
            )
            rows = stored_inputs(agent.inputs.dispositions.root).rows
            original, = (row for row in rows.values() if row.source_text == text)
            assert original.public_status == "not_sent"
            assert not original.has_native_binding and not original.has_started
            if preserved is None:
                assert len(rows) == 1
                preserved = original
            else:
                assert len(rows) == 2 and original.key != preserved.key
                assert rows[preserved.key] == preserved
            assert not agent.turns.turn_state(session_id).busy
            assert not agent.inputs.backend_inboxes
    assert native.session.read_bytes() == saved and native.provider.posts == 0
