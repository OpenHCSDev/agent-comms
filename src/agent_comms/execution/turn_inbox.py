from __future__ import annotations

import asyncio
from abc import (
    ABC,
    abstractmethod,
)
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from agent_comms import agent_events as events
from agent_comms.agent_backend import InputStartListener
from agent_comms.execution.agent_event import AgentEvent
from agent_comms.execution.backend_query import TurnInputContent
from agent_comms.execution.image_input import ImageInput
from agent_comms.execution.pi_vocabulary import ThinkingLevelField
from agent_comms.execution.send_outcome import SendBoundary
from agent_comms.execution.turn_context import InputContributionCoordinates
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import InputId
from agent_comms.registry.thread_model import ThreadModel


class BusyInputPolicy(DeclaredFamily, affix="InputPolicy"):
    """What the backend does with an input that arrives while a run is going."""

    @classmethod
    @abstractmethod
    def streaming(cls) -> type[StreamingInputPolicy]: ...


class StreamingInputPolicy(BusyInputPolicy, declared_name=None):
    """The busy policies Pi's streamingBehavior protocol accepts."""

    @classmethod
    def streaming(cls) -> type[StreamingInputPolicy]:
        return cls


class FollowUpInputPolicy(StreamingInputPolicy, declared_name="followUp"):
    """Run after the current run ends; preserve Pi's exact followUp spelling."""


class SteerInputPolicy(StreamingInputPolicy):
    """Place the input at the next boundary of the current run."""


class RejectInputPolicy(BusyInputPolicy):
    @classmethod
    def streaming(cls) -> type[StreamingInputPolicy]:
        raise ValueError("Pi cannot reject a prompt through streamingBehavior.")


@dataclass(frozen=True, kw_only=True)
class InputContent(TurnInputContent):
    """What one input says: its text, attached images and context contributions."""

    text: str
    images: tuple[ImageInput, ...] = ()
    contributions: tuple[InputContributionCoordinates, ...] = ()

    def acp_prompt_blocks(self) -> list[dict[str, object]]:
        blocks: list[dict[str, object]] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for image in self.images:
            blocks.append({
                "type": "image",
                "mimeType": image.mime_type,
                "data": image.data,
            })
        return blocks


class TurnInboxItem(ABC):
    """An inbox item carries its input identities; the forwarding owner dispatches it."""

    def return_to(self, inbox: TurnInbox) -> None:
        """An operation not handed off still belongs at the inbox front."""
        inbox.begin(self)

    @property
    def carried_inputs(self) -> tuple[InputId, ...]:
        return ()

    def apply_to_acp_turn(
        self,
        *,
        steer: Callable[[list[dict[str, object]]], None],
        send_boundary: SendBoundary,
        input_start: InputStartListener,
    ) -> Iterator[AgentEvent]:
        """Inject this inbox item into a live ACP turn if it carries a user input."""
        del steer, send_boundary, input_start
        return iter(())


class TurnInbox(asyncio.Queue[TurnInboxItem]):
    """The live inbox parks its head until the existing observer sees READY."""

    # CPython owns these queue hooks; its public typeshed stub omits them.
    _queue: deque[TurnInboxItem]
    _getters: deque[asyncio.Future[None]]
    _wakeup_next: Callable[[deque[asyncio.Future[None]]], None]

    def __init__(self) -> None:
        super().__init__()
        self.ready = asyncio.Event()
        self.ready.set()

    async def get(self) -> TurnInboxItem:
        await self.ready.wait()
        return await super().get()

    @property
    def has_ready_work(self) -> bool:
        """Only an open inbox can give queued work to the running turn."""
        return self.ready.is_set() and not self.empty()

    @property
    def ready_inputs(self) -> tuple[InputId, ...]:
        """Runnable user inputs, excluding parked input and control operations."""
        return (
            tuple(input_id for item in self._queue for input_id in item.carried_inputs)
            if self.has_ready_work
            else ()
        )

    def begin(self, item: TurnInboxItem) -> None:
        self._queue.appendleft(item)
        self._wakeup_next(self._getters)

    def hold(self, item: TurnInboxItem) -> None:
        # Park under the shared wire lock, before READY can be published.
        self.ready.clear()
        self.begin(item)


@dataclass(frozen=True, kw_only=True)
class InputRequest(TurnInboxItem):
    """One queued user input handed to a backend: identity, content and busy policy.

    Routing, goal and bus-delivery provenance stay in Core, keyed by ``input_id``.
    """

    input_id: InputId
    content: InputContent
    when_busy: type[BusyInputPolicy]

    @property
    def carried_inputs(self) -> tuple[InputId, ...]:
        return (self.input_id,)

    def apply_to_acp_turn(
        self,
        *,
        steer: Callable[[list[dict[str, object]]], None],
        send_boundary: SendBoundary,
        input_start: InputStartListener,
    ) -> Iterator[AgentEvent]:
        native_id = InputId.mint()
        with send_boundary.send_following(
            self.input_id, native_id, self.content.text
        ) as outcome:
            refusal = outcome.run_direct(
                on_granted=lambda: (
                    input_start.following(self.input_id, native_id, self.content.text),
                    "",
                )[1],
                on_denied=lambda reason: reason,
            )
        if refusal:
            yield events.FollowingInputRefused(id=self.input_id)
            return
        steer(self.content.acp_prompt_blocks())
        yield events.FollowingInputStarted(id=self.input_id)


@dataclass(frozen=True)
class SendNow(TurnInboxItem):
    """Ask the running turn to place these queued steering inputs immediately.

    Only a backend with the :class:`Interrupts` role accepts it.
    """

    input_ids: tuple[InputId, ...]


@dataclass(frozen=True)
class UseSettings(TurnInboxItem):
    """Ask the running turn to use the thread's stored model and thinking level.

    The backend applies them from its next model call; the call in flight
    finishes on the settings it started with.
    """

    model: ThreadModel
    thinking_level: ThinkingLevelField


@dataclass(frozen=True)
class ClearQueuedInputs(TurnInboxItem):
    """Clear only these queued user inputs, never delivered bus inputs."""

    input_ids: tuple[InputId, ...]
