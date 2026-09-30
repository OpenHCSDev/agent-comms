"""Retained actual Pi -> comms_send CLI -> original source -> ACP cold replay."""

import asyncio
import json
import os
import sys
from pathlib import Path

import acp
from acp.schema import TextContentBlock

from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_saved_native_send_has_one_original_source_and_target_outcome(
    native_backend, monkeypatch
):
    native = native_backend
    assert (await native.run("Retained original context before outbound tool"))[-1].ok
    await native.persistent.close()
    original_native = native.session.read_bytes()
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]).resolve(strict=True)
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    comms = Comms(native.root)
    owner = canonical_agent(
        comms,
        auto_wake=False,
        runtime_enabled=True,
        agent_args=[
            "--provider=response-local",
            "--model=fixture",
            "--thinking=off",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-builtin-tools",
            "--extension",
            str(package / "agent-comms-extensions/global-agent-comms/index.mjs"),
        ],
    )
    comms.registry.declare(
        Thread(
            "receiver",
            frozenset(),
            str(native.project),
            model="response-local/fixture",
            thinking_level="off",
        )
    )
    snapshots = asyncio.Queue()
    updates = []

    class Client(acp.Client):
        async def session_update(self, session_id, update, **kwargs):
            updates.append(update)
            for fact in decode_updates(update.field_meta):
                if isinstance(fact, TranscriptSnapshotUpdate):
                    snapshots.put_nowait(fact.page)

    connections = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            await acp.run_agent(owner, writer, reader)
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    client = acp.connect_to_agent(Client(), writer, reader)
    try:
        async with asyncio.timeout(45):
            await client.initialize(protocol_version=1)
            created = await client.new_session(cwd=str(native.project), mcp_servers=[])
            sender = created.session_id
            comms.threads.attach_session(sender, str(native.session))
            await client.load_session(cwd=str(native.project), session_id=sender, mcp_servers=[])
            await snapshots.get()
            open_source = comms.transcripts.capture_page_read(sender)
            native.provider.tool_call = (
                "comms_send",
                {
                    "from": sender,
                    "to": "receiver",
                    "body": "Original outbound ownership handoff",
                },
            )
            try:
                await client.prompt(
                    session_id=sender,
                    prompt=[
                        TextContentBlock(
                            type="text", text="!agent Send the original ownership handoff once"
                        )
                    ],
                )
            except acp.RequestError as failure:
                raise AssertionError(f"Actual ACP error data: {failure.data!r}") from failure
            wire = comms.bus.log.full_history()
            originals = [
                message for message in wire if message.body == "Original outbound ownership handoff"
            ]
            assert len(originals) == 1
            original = originals[0]
            assert not open_source.current()
            page = comms.transcripts.capture_page_read(sender).read()
            sent = [event for event in page.events if event.source == original.reference]
            assert len(sent) == 1 and sent[0].declared_name == "sent"
            assert sent[0].timestamp == original.timestamp
            assert sent[0].routing.reply.targets == ("receiver",)
            assert native.session.read_bytes().startswith(original_native)
            entries = [json.loads(line) for line in native.session.read_text().splitlines()]
            tools = [
                row["message"]
                for row in entries
                if row.get("type") == "message"
                and row["message"].get("role") == "toolResult"
                and row["message"].get("toolName") == "comms_send"
            ]
            assert len(tools) == 1 and not tools[0].get("isError")
            receipt = json.loads(tools[0]["content"][0]["text"])
            assert receipt == {"id": original.message_id}
            await client.load_session(cwd=str(native.project), session_id=sender, mcp_servers=[])
            replay = await snapshots.get()
            assert [event for event in replay.events if event.source == original.reference] == sent
            assert Comms(native.root).transcripts.capture_page_read(sender).read() == page
            assert len(native.saved_inputs()) == 2
            assert native.provider.posts == 3
    finally:
        writer.close()
        await writer.wait_closed()
        await client.close()
        server.close()
        await server.wait_closed()
        await owner.shutdown()
        for task in tuple(connections):
            task.cancel()
        await asyncio.gather(*tuple(connections), return_exceptions=True)
