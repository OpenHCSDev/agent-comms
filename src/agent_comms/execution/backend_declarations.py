"""Lightweight AgentBackend and RuntimeLocation wire declarations.

Imported by ``registry.threads`` before ``AgentBackend.seal()`` so read-only CLI
verbs and wire decoders know every backend name, provider routing, forkability,
and location type without importing heavy runtime execution modules
(``pi_native_backend``, ``antigravity_backend``, ``pi_durable_backend``).

Each served runtime module subclasses its declaration via ``ServedAgentBackend``
and ``ServedRuntimeLocation`` (``metaclass=ServesDeclaration``), taking over the
registered slot when first imported.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.agent_backend import (
    Compacts,
    DeclaredAgentBackend,
    DeclaredForkingBackend,
    DeclaredNativeCutLocation,
    DeclaredSavedModelLocation,
    DeclaredThreadServingBackend,
    ExplicitlyRegisteredBackend,
    Forks,
    Interrupts,
    NewThreadBackend,
    OwnedInboxDrainer,
    RuntimeLocation,
    ThreadServingBackend,
    ThreadSettingApplier,
    TurnInputAcknowledger,
    TurnTranscriptSink,
)
from agent_comms.execution.native_arguments import AntigravityProvider
from agent_comms.execution.session_revision import SessionObservation
from agent_comms.execution.unstarted_runtime import ExternalTranscriptLocation
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import AcpSessionId, TurnId
from agent_comms.registry.thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class PiSessionFileDeclaration(
    DeclaredNativeCutLocation,
    DeclaredSavedModelLocation,
    declared_name="pi_session_file",
):
    """Wire declaration for Pi's stored JSONL session file."""

    path: str

    def select_session(self, directory, execution, source_reads, *, participant):
        return self._served().select_session(
            directory, execution, source_reads, participant=participant
        )

    def imported_sources(self):
        return self._served().imported_sources()

    def session_observation(self, root: Path, thread) -> SessionObservation:
        return self._served().session_observation(root, thread)


class PiNativeBackendDeclaration(
    DeclaredThreadServingBackend,
    DeclaredForkingBackend,
    Forks,
    Interrupts,
    NewThreadBackend,
    declared_name="pi_native",
):
    """Wire declaration for the native Pi RPC backend."""

    location_type: ClassVar[type[RuntimeLocation]] = PiSessionFileDeclaration
    runtime_module: ClassVar[str] = "agent_comms.pi_native_backend"


@dataclass(frozen=True)
class AntigravitySessionDeclaration(
    ExternalTranscriptLocation,
    DeclaredSavedModelLocation,
    declared_name="antigravity_session",
):
    """Wire declaration for an Antigravity ACP conversation database."""

    path: str

    def last_written(self) -> float:
        return self._served().last_written()


class AntigravityAcpBackendDeclaration(
    DeclaredThreadServingBackend,
    DeclaredForkingBackend,
    ExplicitlyRegisteredBackend,
    Forks,
    Interrupts,
    ThreadServingBackend,
    declared_name="antigravity_acp",
):
    """Wire declaration for the multiplexed Antigravity ACP backend."""

    location_type: ClassVar[type[RuntimeLocation]] = AntigravitySessionDeclaration
    provider_type: ClassVar[type[AntigravityProvider]] = AntigravityProvider
    runtime_module: ClassVar[str] = "agent_comms.execution.antigravity_backend"

    @classmethod
    async def apply_setting[Configuration, Command](
        cls,
        owner: ThreadSettingApplier[Configuration, Command],
        configuration: Configuration,
        thread,
        command: Command,
    ) -> None:
        del configuration
        await owner.store_direct_setting(thread, command)

    @classmethod
    async def drain_owned_inbox(
        cls,
        inputs: OwnedInboxDrainer,
        session_id: AcpSessionId,
        thread,
    ) -> None:
        await inputs.drain_direct_inbox(session_id, thread)

    @classmethod
    def acknowledge_turn_inputs(
        cls,
        acknowledger: TurnInputAcknowledger,
        thread,
        origins,
    ) -> None:
        cls.served().acknowledge_turn_inputs(acknowledger, thread, origins)

    @classmethod
    def ignore_turn_inputs(
        cls,
        acknowledger: TurnInputAcknowledger,
        thread,
        origins,
    ) -> None:
        cls.served().ignore_turn_inputs(acknowledger, thread, origins)

    @classmethod
    def turn_transcript_sink(
        cls,
        root: Path,
        incarnation: ThreadIncarnation,
        turn_id: TurnId,
        opening: tuple[MessageReference, ...],
    ) -> TurnTranscriptSink:
        return cls.served().turn_transcript_sink(root, incarnation, turn_id, opening)

    @classmethod
    def join_forking_caller(cls, thread, join_child: object) -> None:
        """Multiplexed Antigravity ACP sessions share the host MCP grant and act as their own thread."""
        del thread, join_child


@dataclass(frozen=True)
class DurableDatabaseDeclaration(DeclaredNativeCutLocation, declared_name="durable_database"):
    """Wire declaration for Pi Durable's SQLite session database."""

    path: str

    def __post_init__(self) -> None:
        if not Path(self.path).is_absolute():
            raise ValueError("Durable database path must be absolute")

    def require_forkable(self) -> None:
        raise ValueError("Durable database forking is not implemented")

    @property
    def session(self) -> Path:
        return Path(self.path)

    @property
    def writer_lock(self) -> Path:
        return self.session.with_suffix(self.session.suffix + ".lock")


class PiDurableBackendDeclaration(
    DeclaredAgentBackend,
    Compacts,
    declared_name="pi_durable",
):
    """Wire declaration for the opt-in Pi Durable backend."""

    location_type: ClassVar[type[RuntimeLocation]] = DurableDatabaseDeclaration
    runtime_module: ClassVar[str] = "agent_comms.execution.pi_durable_backend"
