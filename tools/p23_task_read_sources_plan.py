"""Put retained conversation/input reads and identity lookup on their real providers."""

import json

from p23_revision_owner_plan import ROOT, SRC, imported, patch, stage

bus = SRC / "bus.py"
inputs = SRC / "input_disposition.py"
facts = SRC / "retained_task_facts.py"
publication = SRC / "bus_publication.py"
read_source = '''"""Original message-read capabilities, independent of live bus/table initialization."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import MessageId, ThreadLookup


class ConversationRead(ABC):
    __slots__ = ()

    @abstractmethod
    def conversation(
        self, lookup: ThreadLookup, *, after: int = 0, before: int | None = None,
        limit: int = 100, descending: bool = True, sequences: tuple[int, ...] = (),
    ) -> tuple[messages.Message, ...]: ...


class MessageReferencesRead(ABC):
    __slots__ = ()

    @abstractmethod
    def messages_for_references(
        self, references: Iterable[MessageReference],
    ) -> tuple[messages.Message, ...]: ...


class MessageIdentityRead(ABC):
    __slots__ = ()

    @abstractmethod
    def contains_message_identity(self, sender: str, message_id: MessageId) -> bool: ...


import agent_comms.messaging.messages as messages
'''
lookup_source = '''"""Lookup on the original input provider, distinct from ordered batch capture."""

from abc import ABC, abstractmethod

from agent_comms.input_key import InputKey


class InputLookup[Attempt](ABC):
    __slots__ = ()

    @abstractmethod
    def lookup(self, key: InputKey) -> Attempt: ...
'''
regression = '''"""Retained reads use original nominal providers without weakening source records."""

from inspect import isabstract
from typing import get_args, get_origin, get_type_hints

from agent_comms.bus import Bus, BusReader, BusSnapshot
from agent_comms.bus_publication import Delivered
from agent_comms.input_attempt import InputAttempt, StoredInput
from agent_comms.input_disposition import InputDispositions, InputDocument
from agent_comms.execution.input_lookup import InputLookup
from agent_comms.messaging.message_reads import ConversationRead, MessageIdentityRead, MessageReferencesRead
from agent_comms.messaging.messages import ClaimMessage, Message
from agent_comms.retained_task_facts import (
    ClaimTaskFact, InputTaskFact, RetainedTaskFacts, UserSourceTaskFact,
)
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry


def test_retained_read_uses_the_actual_message_and_input_providers():
    assert issubclass(BusReader, ConversationRead)
    assert issubclass(BusReader, MessageReferencesRead)
    assert issubclass(BusReader, MessageIdentityRead)
    assert not isabstract(Bus)
    assert not isabstract(BusSnapshot)
    assert get_type_hints(RetainedTaskFacts.capture)["source"] is ConversationRead
    alias = get_type_hints(RetainedTaskFacts.original_input_facts)["inputs"]
    assert get_origin(alias) is InputLookup
    assert get_args(alias) == (InputAttempt,)
    assert issubclass(InputDocument, InputLookup)
    assert issubclass(InputDispositions, InputLookup)
    assert not isabstract(InputDocument)
    assert not isabstract(InputDispositions)


def test_original_fact_source_fields_remain_concrete():
    assert get_type_hints(UserSourceTaskFact)["source"] is Message
    assert get_type_hints(ClaimTaskFact)["source"] is ClaimMessage
    assert get_type_hints(InputTaskFact)["source"] is StoredInput
    assert get_type_hints(BusReader.conversation)["return"] == tuple[Delivered, ...]
    assert get_type_hints(ConversationRead.conversation)["return"] == tuple[Message, ...]


def test_retained_applicability_uses_existing_task_owners():
    hints = get_type_hints(RetainedTaskFacts.current_authored_lineages)
    assert hints["owner"] is TaskOwner
    assert get_origin(hints["registry"]) is TaskRegistry
    assert get_args(hints["registry"]) == (TaskOwner,)
'''
facts_before = facts.read_text()
facts_after = facts_before.replace("from agent_comms.bus import BusReader\n", "").replace(
    "from agent_comms.input_attempt import StoredInput", "from agent_comms.input_attempt import InputAttempt, StoredInput",
).replace("from agent_comms.input_disposition import InputDispositions\n", "").replace(
    "from agent_comms.registry.registry_document import RegistrySnapshot\n", "",
).replace("from agent_comms.registry.threads import Thread\n", "").replace(
    "source: BusReader", "source: ConversationRead",
).replace("inputs: InputDispositions", "inputs: InputLookup[InputAttempt]").replace(
    "owner: Thread, registry: RegistrySnapshot", "owner: TaskOwner, registry: TaskRegistry[TaskOwner]",
)

source_stage = stage(
    "p23-original-retained-read-providers-and-database-identity-owner",
    "The pinned 149-class trace identifies actual eager bus/table imports, not eager concrete fact fields. Retained capture needs only original ascending conversation records; input facts need lookup, not OriginalInputs.originals. Add state-free nominal capabilities to actual BusReader/InputDocument/InputDispositions and retain concrete Delivered/Message/ClaimMessage/StoredInput results and fields. Reuse existing TaskOwner/TaskRegistry for applicability. Move the identical sender AND message_id existence query under BusReader.observing; HumanOrigin retains its refusal policy. SQLiteReader.reader_snapshot always rolls back in finally and CoordinationReadUnavailable.checking catches only sqlite OperationalError; HumanSendRefusedError is not intercepted, and BusSnapshot.observing only yields its held connection. Thus moving the pure existence result to the reader leaves connection acquisition/query/finalization and the policy exception identity unchanged. No writer, snapshot acquisition, scheduler, constructor or generic wire model changes. This closes only the retained live-read boundary, not the remaining origin/value/stronger native-input closure. Native exact patches require original consumer, cold, concrete-field and source-hidden validation.",
    [
        {"operation": "create_file", "file_path": str(SRC / "message_reads.py"), "source": read_source},
        {"operation": "create_file", "file_path": str(SRC / "input_lookup.py"), "source": lookup_source},
        patch(bus, [
            ("class BusReader(RouteCounts, ABC):",
             "class BusReader(RouteCounts, ConversationRead, MessageReferencesRead, MessageIdentityRead, ABC):"),
            ("    def revision(self) -> int:\n        with self.observing() as db:\n            return BusRow.last_seq(db)",
             "    def contains_message_identity(self, sender: str, message_id: MessageId) -> bool:\n        with self.observing() as db:\n            return MessageIdentitySource.exists(db, sender=sender, message_id=message_id)\n\n    def revision(self) -> int:\n        with self.observing() as db:\n            return BusRow.last_seq(db)"),
        ]),
        imported(bus, "from agent_comms.messaging.message_reads import ConversationRead, MessageIdentityRead, MessageReferencesRead"),
        imported(bus, "from agent_comms.nominal.field_codec import MessageId"),
        patch(inputs, [
            ("class InputDocument(OriginalInputs):", "class InputDocument(OriginalInputs, InputLookup[InputAttempt]):"),
            ("class InputDispositions(DurableStore, CompleteInputReader):",
             "class InputDispositions(DurableStore, CompleteInputReader, InputLookup[InputAttempt]):"),
        ]),
        imported(inputs, "from agent_comms.execution.input_lookup import InputLookup"),
        patch(facts, [(facts_before, facts_after)]),
        imported(facts, "from agent_comms.messaging.message_reads import ConversationRead"),
        imported(facts, "from agent_comms.execution.input_lookup import InputLookup"),
        imported(facts, "from agent_comms.registry.task_owner import TaskOwner"),
        imported(facts, "from agent_comms.registry.task_registry import TaskRegistry"),
        patch(publication, [
            ("        with source.observing() as db:\n            if MessageIdentitySource.exists(db, sender=sender, message_id=message_id):\n                raise HumanSendRefusedError(RelationViolationError(\n                    \"Human message identity was already published.\"))",
             "        if source.contains_message_identity(sender, message_id):\n            raise HumanSendRefusedError(RelationViolationError(\n                \"Human message identity was already published.\"))"),
        ]),
        {"operation": "remove_import_names", "file_path": str(publication), "module_name": "agent_comms.bus_tables", "import_names": ["MessageIdentitySource"]},
        {"operation": "create_file", "file_path": str(ROOT / "tests/test_retained_task_read_contracts.py"), "source": regression},
    ],
)
recipe, = source_stage["recipes"]
changes = [operation for operation in recipe["operations"]
           if operation["operation"] not in ("ensure_import", "remove_import_names")]
imports = [operation for operation in recipe["operations"]
           if operation["operation"] in ("ensure_import", "remove_import_names")]
print(json.dumps({"stages": [
    stage(recipe["recipe_id"], recipe["reason"], changes),
    stage("p23-bind-original-retained-read-providers", recipe["reason"], imports),
]}, indent=2))
