"""Typed live ACP text and unconditional saved-history snapshot publication."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from functools import partial


from agent_comms.acp_extension import TranscriptSnapshotUpdate, TurnChangedUpdate
from agent_comms.comms import Comms
from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.controller import SessionUpdateSink
from agent_comms.execution.session_updates import encode_updates
from agent_comms.execution.turn_lease import TurnState
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.transport.tools.reads import TranscriptPageRead
from agent_comms.ui_model.observation import ReadTranscript

@dataclass(frozen=True, kw_only=True)
class TranscriptUpdate(DeclaredFamily, affix="TranscriptUpdate"):
    @abstractmethod
    async def publish(
        self, session_id: AcpSessionId, client: SessionUpdateSink,
    ) -> None: ...


@dataclass(frozen=True, kw_only=True)
class TurnTranscriptUpdate(TranscriptUpdate):
    state: TurnState

    async def publish(
        self, session_id: AcpSessionId, client: SessionUpdateSink,
    ) -> None:
        await client.publish_facts(
            TurnChangedUpdate(self.state), session_id=session_id,
        )


class TranscriptReplay:
    """The agent reads saved history from the Comms it already holds and publishes one
    snapshot; the read's kind decides how contention is answered."""

    def __init__(self, comms: Comms):
        self.comms = comms

    async def replay(
        self, thread: str, *, session_id: AcpSessionId, client: SessionUpdateSink,
        read: type[ReadTranscript] = ReadTranscript,
    ) -> None:
        snapshot = await read(
            str(self.comms.root), TranscriptPageRead(thread=thread),
        ).answered(self.comms)
        await self._publish(session_id, snapshot, client)

    async def _publish(
        self,
        session_id: AcpSessionId,
        snapshot: TranscriptSnapshotUpdate,
        destination: SessionUpdateSink,
    ) -> None:
        metadata = await Coordination.run_worker(partial(encode_updates, snapshot))
        await destination.publish_metadata(session_id=session_id, metadata=metadata)
