"""Thread launch environment encoding and decoding, kept out of light runtime startup."""

from __future__ import annotations

import os
from abc import ABC
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Annotated

from agent_comms.execution.launch_runtime import (
    CallingThreadUnavailable,
    EnvironmentRecord,
    EnvironmentValue,
    FlagText,
    TagListText,
    UnsetVariable,
)
from agent_comms.nominal.field_codec import FieldCodec, WireName, WireOmitDefault, boundary_decoder
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_text import (
    NoParent,
    NoThreadText,
    ThreadParent,
    ThreadText,
)


class ThreadLaunchSource(ABC):
    """Fields a registered thread supplies when building its launch environment."""

    __slots__ = ()
    name: str
    worktree: Checkout
    tags: frozenset[str]
    parent: ThreadParent
    task: ThreadText


@dataclass(frozen=True)
class ThreadLaunch(EnvironmentRecord):
    """The environment that carries one thread's launch into its processes.

    Launch variable spellings for a thread belong to these fields; writers encode
    the record and readers decode it.
    """

    name: Annotated[str, WireName("AGENT_COMMS_THREAD")]
    worktree: Annotated[Checkout, WireName("PI_WORKTREE")]
    tags: Annotated[
        frozenset[str],
        TagListText,
        WireOmitDefault(),
        WireName("AGENT_COMMS_TAGS"),
    ] = frozenset()
    parent: Annotated[ThreadParent, WireOmitDefault(), WireName("PI_PARENT_ID")] = NoParent()
    task: Annotated[ThreadText, WireOmitDefault(), WireName("PI_TASK")] = NoThreadText()
    managed: Annotated[
        bool,
        FlagText,
        WireOmitDefault(),
        WireName("AGENT_COMMS_MANAGED"),
    ] = False
    startup_input: Annotated[
        EnvironmentValue,
        WireOmitDefault(),
        WireName("AGENT_COMMS_STARTUP_INPUT_KEY"),
    ] = UnsetVariable()

    @classmethod
    @boundary_decoder
    def from_thread(cls, thread: ThreadLaunchSource) -> ThreadLaunch:
        return cls(
            thread.name,
            thread.worktree,
            frozenset(thread.tags),
            thread.parent,
            thread.task,
        )

    @classmethod
    def current(cls, environment: Mapping[str, str] = os.environ) -> ThreadLaunch:
        """Decode the explicitly declared calling thread and project."""
        values = cls.inherited(environment)
        if not values.get(cls.variable("name")):
            raise CallingThreadUnavailable(f"Calling thread requires {cls.variable('name')}.")
        if not values.get(cls.variable("worktree")):
            raise CallingThreadUnavailable(f"Calling thread requires {cls.variable('worktree')}.")
        return FieldCodec.decode(cls, values)

    @classmethod
    def called_by(
        cls,
        name: str,
        canonicalize: Callable[[str], str],
        *,
        environment: Mapping[str, str] = os.environ,
    ) -> bool:
        try:
            caller = cls.current(environment).name
        except CallingThreadUnavailable:
            return False
        return canonicalize(caller) == name

    def take_startup_input(self, environment: MutableMapping[str, str]) -> str | None:
        """Consume this already-decoded launch token before any child inherits it."""
        self.strip(environment, "startup_input")
        return self.startup_input.otherwise(None)

    def apply(self, environment: dict[str, str]) -> dict[str, str]:
        """Replace any inherited launch with this one."""
        return self.strip(environment) | self.encode()
