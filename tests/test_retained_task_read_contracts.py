"""Retained reads use original nominal providers without weakening source records."""

from inspect import isabstract
from typing import get_args, get_origin, get_type_hints

from agent_comms.execution.input_lookup import InputLookup
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.bus import BusReader
from agent_comms.messaging.bus import BusSnapshot
from agent_comms.messaging.bus_publication import Delivered
from agent_comms.input_attempt import InputAttempt, StoredInput
from agent_comms.input_disposition import InputDispositions, InputDocument
from agent_comms.messaging.message_reads import (
    ConversationRead,
    MessageIdentityRead,
    MessageReferencesRead,
)
from agent_comms.messaging.messages import BusRecord, ClaimMessage, Message
from agent_comms.messaging.retained_task_facts import ClaimTaskFact
from agent_comms.messaging.retained_task_facts import InputTaskFact
from agent_comms.messaging.retained_task_facts import RetainedTaskFacts
from agent_comms.messaging.retained_task_facts import UserSourceTaskFact
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry


def test_retained_read_uses_the_actual_message_and_input_providers():
    assert issubclass(BusReader, ConversationRead)
    assert issubclass(BusReader, MessageReferencesRead)
    assert issubclass(BusReader, MessageIdentityRead)
    assert not isabstract(Bus)
    assert not isabstract(BusSnapshot)
    conversation = get_type_hints(RetainedTaskFacts.capture)["source"]
    assert get_origin(conversation) is ConversationRead
    assert get_args(conversation) == (BusRecord,)
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
    assert issubclass(Delivered, BusRecord)


def test_retained_applicability_uses_existing_task_owners():
    hints = get_type_hints(RetainedTaskFacts.current_authored_lineages)
    assert hints["owner"] is TaskOwner
    assert get_origin(hints["registry"]) is TaskRegistry
    assert get_args(hints["registry"]) == (TaskOwner,)
