"""Backend input records, busy policies and the inbox that owns queued work."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .field_codec import InputId
from .image_input import ImageInput
from .pi_vocabulary import ThinkingLevelField
from .thread_model import ThreadModel
from .turn_context import InputContributionCoordinates


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
class InputContent:
    """What one input says: its text, attached images and context contributions."""

    text: str
    images: tuple[ImageInput, ...] = ()
    contributions: tuple[InputContributionCoordinates, ...] = ()


class TurnInboxItem(ABC):
    """An inbox item carries its input identities; the forwarding owner dispatches it."""

    def return_to(self, inbox: TurnInbox) -> None:
        """An operation not handed off still belongs at the inbox front."""
        inbox.begin(self)

    @property
    def carried_inputs(self) -> tuple[InputId, ...]:
        return ()

    def refuse(self, inputs, turn):
        inputs.refuse(turn, self.carried_inputs)


class TurnInbox(asyncio.Queue[TurnInboxItem]):
    """The live inbox parks its head until the existing observer sees READY."""

    _queue: deque[TurnInboxItem]

    def __init__(self):
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
        return tuple(
            input_id for item in self._queue for input_id in item.carried_inputs
        ) if self.has_ready_work else ()

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
class ClearQueue(TurnInboxItem):
    """Clear only these queued user inputs, never delivered bus inputs."""

    input_ids: tuple[InputId, ...]
