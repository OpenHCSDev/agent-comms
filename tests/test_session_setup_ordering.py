"""Real SDK/stdio session setup preserves new-ID admission and known-ID replay order."""

import asyncio
import os
from pathlib import Path
import sys

from acp import spawn_agent_process
from acp.connection import StreamDirection, StreamEvent

from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.launch_root import LaunchRoot
from bus_record_fixture import prepare_native_comms


class SessionPackets:
    def __init__(self):
        self.frames: list[StreamEvent] = []
        self.snapshot_received = asyncio.Event()

    def record(self, event: StreamEvent) -> None:
        self.frames.append(event)

    async def session_update(self, session_id, update, **kwargs):
        if any(isinstance(fact, TranscriptSnapshotUpdate)
               for fact in decode_updates(update.field_meta)):
            self.snapshot_received.set()

    def response_position(self, method: str) -> int:
        request, = (event.message for event in self.frames
                    if event.direction is StreamDirection.OUTGOING
                    and event.message.get("method") == method)
        position, = (position for position, event in enumerate(self.frames)
                     if event.direction is StreamDirection.INCOMING
                     and event.message.get("id") == request["id"]
                     and "result" in event.message)
        return position

    def update_positions(self, session_id: str, *, after: int = -1) -> list[int]:
        return [position for position, event in enumerate(self.frames)
                if position > after and event.direction is StreamDirection.INCOMING
                and event.message.get("method") == "session/update"
                and event.message["params"]["sessionId"] == session_id]


async def test_new_response_precedes_updates_and_load_replays_before_response(tmp_path, native_runtime_package):
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "native-config"
    config.mkdir()
    comms = prepare_native_comms(Comms(tmp_path / "wire"))
    packets = SessionPackets()
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                       AGENT_COMMS_NATIVE_CONFIG_DIR=str(config), PI_CODING_AGENT_DIR=str(config))
    try:
        async with spawn_agent_process(
            packets, sys.executable, "-m", "agent_comms.acp",
            *LaunchRoot.from_path(comms.root).arguments(),
            env=environment, cwd=project, observers=[packets.record], use_unstable_protocol=True,
        ) as (client, _process):
            await client.initialize(protocol_version=1)
            created = await client.new_session(str(project), mcp_servers=[])
            await asyncio.wait_for(packets.snapshot_received.wait(), 15)
            first_update, *_ = packets.update_positions(created.session_id)
            assert packets.response_position("session/new") < first_update

            # Closing joins the original subscriber; its replay cannot masquerade
            # as the following load's replay. The native owner remains running.
            await client.close_session(created.session_id)
            boundary = len(packets.frames) - 1
            packets.snapshot_received.clear()
            await client.load_session(str(project), created.session_id, mcp_servers=[])
            await asyncio.wait_for(packets.snapshot_received.wait(), 15)
            replays = [position for position in packets.update_positions(created.session_id, after=boundary)
                       if any(isinstance(fact, TranscriptSnapshotUpdate) for fact in decode_updates(
                           packets.frames[position].message["params"]["update"].get("_meta")))]
            assert replays and max(replays) < packets.response_position("session/load")
    finally:
        names = tuple(name for thread in comms.registry.all_threads().values()
                      for name in thread.role.executor(thread.name))
        await asyncio.to_thread(comms.owners.stop, *names)
