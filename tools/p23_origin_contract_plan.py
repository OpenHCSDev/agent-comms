"""Bind the moved original origins to their actual nominal read/write providers."""

import json

from p23_revision_owner_plan import SRC, imported, patch, stage

writer = '''"""The original bus append operations used by initial publication."""
from __future__ import annotations

from abc import ABC, abstractmethod
from agent_comms.messaging.bus_record_builder import RecordBuilder
from agent_comms.messaging import messages


class MessageWriter(ABC):
    __slots__ = ()

    @abstractmethod
    def append(self, build: RecordBuilder) -> messages.BusRecord: ...

    @abstractmethod
    def append_original(self, build: RecordBuilder) -> messages.BusRecord: ...
'''
directions = '''"""Actual goal and directive-store capabilities used by authored messages."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Container
from agent_comms.messaging import messages
from agent_comms.owner_requests import OwnerRequestId, RequestDirective


class DirectiveWriter(ABC):
    __slots__ = ()

    @abstractmethod
    def record_directive(self, request: OwnerRequestId, directive: RequestDirective) -> None: ...


class MessageDirections(ABC):
    __slots__ = ()
    registry: Container[str]
    todos: DirectiveWriter

    @abstractmethod
    def direct(self, name: str, message: messages.Message) -> None: ...
'''
origin = SRC / "initial_origin.py"
builder = SRC / "bus_record_builder.py"
publication = SRC / "bus_publication.py"
before = origin.read_text()
after = before.replace("from typing import TYPE_CHECKING\n", "").replace(
    "from agent_comms.messaging.messages import BusRecord, Message\n", "from agent_comms.messaging import messages\n",
)
guard = '''if TYPE_CHECKING:
    from agent_comms.bus import Bus, BusReader
    from agent_comms.messaging.bus_record_builder import RecordBuilder
    from agent_comms.planning.goal_management import Goals
    from agent_comms.registry.registry_document import RegistrySnapshot
    from agent_comms.registry.threads import Thread
'''
if before.count(guard) != 1:
    raise ValueError("The projected original-origin guard changed; retain its new source before composing")
after = after.replace(guard, "")
for old, new in (
    ("message: Message", "message: messages.Message"),
    ("record: BusRecord", "record: messages.BusRecord"),
    (" -> BusRecord", " -> messages.BusRecord"),
    ("sender_thread: Thread", "sender_thread: TaskOwner"),
    ("user: Thread", "user: TaskOwner"),
    ("snapshot: RegistrySnapshot", "snapshot: TaskRegistry[TaskOwner]"),
    ("source: BusReader", "source: MessageIdentityRead"),
    ("bus: Bus", "bus: MessageWriter"),
    ("goals: Goals", "goals: MessageDirections"),
):
    if old not in after:
        raise ValueError(f"Original origin signature is not the admitted source: {old}")
    after = after.replace(old, new)
changes = [
    {"operation": "create_file", "file_path": str(SRC / "message_writer.py"), "source": writer},
    {"operation": "create_file", "file_path": str(SRC / "message_directions.py"), "source": directions},
    patch(origin, [(before, after)]),
    patch(builder, [
        ("from agent_comms.messaging.messages import BusRecord", "from agent_comms.messaging import messages"),
        ("type RecordBuilder = Callable[[int, sqlite3.Connection], BusRecord]",
         "type RecordBuilder = Callable[[int, sqlite3.Connection], messages.BusRecord]"),
    ]),
    patch(SRC / "bus.py", [
        ("    CompleteInputReader, Watched,", "    CompleteInputReader, Watched, MessageWriter,"),
    ]),
    patch(SRC / "planning/goal_management.py", [
        ("    GoalRecords, ProblemGoals, WakeWorkSource[Thread], GoalFork[Thread, GoalDeclaration],",
         "    GoalRecords, ProblemGoals, WakeWorkSource[Thread], GoalFork[Thread, GoalDeclaration], MessageDirections,"),
    ]),
    patch(SRC / "planning/orders.py", [
        ("class TodoStore(OwnerRequestStore, TodoHistory, GoalAssignments):",
         "class TodoStore(OwnerRequestStore, TodoHistory, GoalAssignments, DirectiveWriter):"),
    ]),
    patch(SRC / "registry/registration.py", [("class Registration:", "class Registration(Container[str]):")]),
    patch(SRC / "relationship_catalog_source.py", [
        ("from typing import Protocol", "from abc import ABC"),
        ("class RelationshipCatalogSource(Protocol):\n    @property\n    def catalog(self) -> ChannelCatalog: ...",
         "class RelationshipCatalogSource(ABC):\n    __slots__ = ()\n    catalog: ChannelCatalog"),
    ]),
    patch(SRC / "channel_management.py", [
        ("class ChannelManagement(ChannelOrderSource[type[ThreadSort], type[ChannelSort]]):",
         "class ChannelManagement(ChannelOrderSource[type[ThreadSort], type[ChannelSort]], RelationshipCatalogSource):"),
    ]),
]
imports = [
    imported(origin, "from agent_comms.messaging.bus_record_builder import RecordBuilder"),
    imported(origin, "from agent_comms.messaging.message_directions import MessageDirections"),
    imported(origin, "from agent_comms.messaging.message_reads import MessageIdentityRead"),
    imported(origin, "from agent_comms.messaging.message_writer import MessageWriter"),
    imported(origin, "from agent_comms.registry.task_owner import TaskOwner"),
    imported(origin, "from agent_comms.registry.task_registry import TaskRegistry"),
    imported(SRC / "bus.py", "from agent_comms.messaging.message_writer import MessageWriter"),
    imported(SRC / "planning/goal_management.py", "from agent_comms.messaging.message_directions import MessageDirections"),
    imported(SRC / "planning/orders.py", "from agent_comms.messaging.message_directions import DirectiveWriter"),
    imported(SRC / "registry/registration.py", "from collections.abc import Container"),
    imported(SRC / "channel_management.py", "from agent_comms.registry.relationship_catalog_source import RelationshipCatalogSource"),
]
reason = (
    "Keep the moved original InitialOrigin/OwnerVoiceOrigin/HumanOrigin fields, constructors and bodies. "
    "Ordinary postponed module-qualified Message/BusRecord fields preserve their original class identities without eagerly importing them before Message initialization. "
    "The original Bus implements append/append_original, Goals implements direct and owns registry/todos, TodoStore implements record_directive, and Registration implements __contains__; declare those actual memberships rather than a Protocol, facade, copied record or fake default. "
    "Reuse existing TaskOwner/TaskRegistry for the exact sender/registered-origin reads. "
    "All root/author/birth/worktree refusals, sender AND message_id lookup, append/commit error paths and acquisition order remain at the original owners. "
    "This stage closes the lower origin/write/read and catalog boundaries, not the remaining higher InputOrigin/TaskAttachment callback closure."
)
print(json.dumps({"stages": [
    stage("p23-close-original-origin-and-catalog-contracts", reason, changes),
    stage("p23-bind-original-origin-contract-providers", reason, imports),
]}, indent=2))
