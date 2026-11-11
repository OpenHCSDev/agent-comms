"""The existing turn runner supplies the resources used by one owned turn."""
from __future__ import annotations
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack

from agent_comms.agent_backend import AgentBackend
from agent_comms.execution.controller import Controller
from agent_comms.execution.goal_scheduler import GoalScheduler
from agent_comms.execution.input_drain import BusSessionsRuntime, InputDrain
from agent_comms.execution.native_input_owner import RegistryOwner
from agent_comms.execution.turn_effects import TurnEffects
from agent_comms.execution.turn_lease import TurnLeaseFence
from agent_comms.execution.turn_phase import TurnPhase
from agent_comms.nominal.field_codec import AcpSessionId, TurnId
from agent_comms.execution.pi_events import DialogUiRequest
from agent_comms.execution.pi_events import ExtensionUiChoice
from agent_comms.registry.threads import Thread
from agent_comms.messaging.routing import NoTurnRouting
from agent_comms.messaging.routing import Routing
from agent_comms.transcript.transcript_updates import TurnTranscriptUpdate


class OwnedTurnRunner(BusSessionsRuntime, ABC):
    __slots__ = ()
    goals: GoalScheduler
    inputs: InputDrain
    effects: TurnEffects
    persistent_backends: dict[AcpSessionId, AgentBackend]

    @abstractmethod
    def backend_for(self, session_id: AcpSessionId, thread: Thread) -> AgentBackend: ...

    @abstractmethod
    def acquire_turn(
        self, resources: AsyncExitStack, session_id: AcpSessionId, thread_name: str,
        turn_id: TurnId, detail: str, routing: Routing = NoTurnRouting(),
    ) -> RegistryOwner: ...

    @abstractmethod
    async def transition_turn(
        self, session_id: AcpSessionId, lease: TurnLeaseFence, phase: TurnPhase,
    ) -> None: ...

    @abstractmethod
    def current_turn_update(self, session_id: AcpSessionId) -> TurnTranscriptUpdate: ...

    @abstractmethod
    async def extension_ui_permission(
        self, session_id: AcpSessionId, turn_id: TurnId,
        controller: Controller, request: DialogUiRequest,
    ) -> ExtensionUiChoice: ...
