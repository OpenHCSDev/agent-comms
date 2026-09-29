"""Offline native selected summary, real ACP queue, strict reopen and original/followup once."""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    QueuePromptRequest,
    decode_updates,
    encode_request,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.selected_pi_summary_rpc import SelectedSummarySlot
from agent_comms.threads import Thread
from test_selected_owner_compaction_integration import owner_fixture

pytestmark = pytest.mark.skipif(
    not os.environ.get("PI_COMPACTION_TEST_PACKAGE"), reason="Prepared native bundle required"
)


@pytest.mark.parametrize("foreign", [False, True])
async def test_actual_acp_queued_during_summary_runs_once_after_original(
    tmp_path, monkeypatch, foreign
):
    async with owner_fixture(tmp_path, monkeypatch, real_host=True, goal=False) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", info.model)
        comms = wire(tmp_path / "acp-wire")
        root_id = comms.messaging.initialize_private_initial_protocol()
        project = tmp_path / "proj"
        project.mkdir()
        agent = CommsAgent(
            comms,
            agent_bin=launcher,
            agent_args=[],
            runtime_enabled=True,
            auto_wake=False,
            private_nk_native_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
            private_nk_wire_root_id=root_id,
        )
        updates = []

        class Client:
            async def session_update(self, **kwargs):
                updates.append(kwargs["update"])

        agent.on_connect(Client())
        await agent.new_session(cwd=str(project), mcp_servers=[])
        agent.inputs.drain_tasks["proj"].cancel()
        await asyncio.gather(agent.inputs.drain_tasks["proj"], return_exceptions=True)
        comms.registry.register(
            replace(
                comms.registry.require("proj"),
                session_file=file,
                model=info.model,
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
        comms.agents.set_agent_info(
            "proj", model=info.model, context_used=info.context_used, context_size=info.context_size
        )
        agent.turns.persistent_backends["proj"] = persistent
        selected_exchange = SelectedSummarySlot.run_selected_summary
        accepted = []
        operations = []

        async def summary_with_queue(slot, *args, **kwargs):
            # Source was captured; selected summary has not finished or committed.
            response = await agent.prompt(
                "proj",
                [{"type": "text", "text": "Fresh followup"}],
                field_meta=encode_request(QueuePromptRequest(defer_display=True)),
            )
            receipt = next(
                update
                for update in decode_updates(response.field_meta)
                if isinstance(update, InputDeliveryChangedUpdate)
            )
            key = "acp:" + receipt.input_id
            accepted.append(key)
            row = agent.inputs.dispositions.read().rows.get(key)
            assert not row.has_native_binding and row.accepts_reservation
            if foreign:
                for name in ("foreign", "another"):
                    comms.registry.declare(Thread(name, frozenset(), str(project)))
                comms.messaging.send("foreign", "another", "unrelated ingress")
                agent.inputs.dispositions.record(
                    "acp:foreign",
                    seq=None,
                    owner="foreign",
                    admission=1,
                    target="foreign",
                    text="foreign input",
                )
            result = await selected_exchange(slot, *args, **kwargs)
            operations.append(result.operation_id)
            return result

        monkeypatch.setattr(SelectedSummarySlot, "run_selected_summary", summary_with_queue)
        managed = NativePiRpcLaunch.managed

        def offline_launch(command, arguments, **kwargs):
            # Preserve production attestation and session arguments; run the
            # prepared native SDK/RPC with its network-prohibited local model.
            launch = managed(command, arguments, **kwargs)
            return replace(
                launch,
                argv=(
                    "node",
                    str(
                        Path(__file__).resolve().parents[1]
                        / "stack/test-native-selected-owner-host.mjs"
                    ),
                    "--session",
                    launch.session_file,
                ),
                env=dict(launch.env, PR95_OWNER_FIXTURE_ROOT=str(tmp_path)),
            )

        monkeypatch.setattr(NativePiRpcLaunch, "managed", offline_launch)
        try:
            async with asyncio.timeout(35):
                await agent.prompt("proj", [{"type": "text", "text": "Original after summary"}])
            rows = agent.inputs.dispositions.read().rows
            own = [row for row in rows.values() if row.owner == "proj"]
            assert len(own) == 2
            assert all(row.declared_name == "started" for row in own), (own, updates)
            assert len({row.native_id for row in own}) == 2
            journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
            assert len(operations) == 1
            assert journal.summaries.get(operations[0]).state.declared_name == "linked"
            assert not journal.summaries.blocking(file)
            entries = [json.loads(line) for line in Path(file).read_text().splitlines()]
            compact = [i for i, row in enumerate(entries) if row["type"] == "compaction"]
            assert len(compact) == 1
            original = next(row for row in own if row.key != accepted[0])
            followup = rows[accepted[0]]
            positions = []
            for row in (original, followup):
                starts = [
                    i
                    for i, event in enumerate(entries)
                    if event["type"] == "message"
                    and event["message"].get("inputId") == row.native_id
                ]
                assert len(starts) == 1
                positions.append(starts[0])
            assert compact[0] < positions[0] < positions[1]
            assert not agent.inputs.queued_inputs.get("proj")
            if foreign:
                assert rows["acp:foreign"].declared_name == "reserved"
        finally:
            await agent.shutdown()
