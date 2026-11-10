"""Use original lazy fact records and actual nominal callback providers for task methods."""

import json
import re

from p23_revision_owner_plan import SRC, imported, patch, stage

callbacks = '''"""Callback memberships consumed by original task attachments."""
from abc import ABC, abstractmethod
from agent_comms.execution.turn_context import ContextSegment


class DeliveredContext(ABC):
    __slots__ = ()

    @abstractmethod
    def delivered(self) -> ContextSegment: ...


class TeamObservation(ABC):
    """Only declared retained teammate observations belong to this result set."""
    __slots__ = ()
'''
reason = (
    "The complete retained task/fact trace finds no local Message, ClaimMessage, StoredInput or InputAttempt bindings: their uses here are the original source annotations. "
    "Write those original classes through ordinary module-qualified bindings, preserving the exact FieldCodec classes, all fields and default factories. "
    "This authored bounded spelling change is not a general lexical resolver or a proof for arbitrary modules. "
    "Use the existing DeliversInput on its actual CliBridge provider after native relocation below Message initialization. "
    "MessageDelivery supplies the delivered-context callback and the original TeamUpdate supplies teammate-observation membership; state-free ABCs neither copy their records nor add state. "
    "Goals supplies MessageDirections and original input providers supply InputLookup. "
    "Original retained-fact constructor calls become ordinary calls through the original module binding, with the same constructors, arguments and order. "
    "The separately owned NativeInputConstraintPin acquisition hunk must retain its stronger privacy/input scope; conversation/reference reads alone do not close it."
)
facts = SRC / "retained_task_facts.py"
fb = facts.read_text()
fa = fb.replace("from agent_comms.input_attempt import InputAttempt, StoredInput\n", "").replace(
    "from agent_comms.messaging.messages import ClaimMessage, Message\n", "",
)
for name in ("InputAttempt", "StoredInput"):
    fa = re.sub(rf"\b{name}\b", f"input_attempt.{name}", fa)
for name in ("ClaimMessage", "Message"):
    fa = re.sub(rf"\b{name}\b", f"messages.{name}", fa)

tasks = SRC / "task_sources.py"
tb = tasks.read_text()
guard = '''if TYPE_CHECKING:
    from agent_comms.bus import BusReader
    from agent_comms.cli_bridges.shared import CliBridge
    from agent_comms.context_segments.wake import MessageDelivery
    from agent_comms.input_attempt import StoredInput
    from agent_comms.input_disposition import InputDispositions
    from agent_comms.messaging.messages import Message
    from agent_comms.planning.goal_management import Goals
    from agent_comms.retained_task_facts import RetainedTaskFacts
    from agent_comms.task_attachments.team_pings import TeamUpdate
'''
if tb.count(guard) != 1:
    raise ValueError("The original task annotation guard changed before integration")
ta = tb.replace(guard, "").replace("from typing import TYPE_CHECKING, Annotated, Self", "from typing import Annotated, Self")
for name, replacement in (
    ("Message", "messages.Message"), ("StoredInput", "input_attempt.StoredInput"),
    ("RetainedTaskFacts", "task_facts.RetainedTaskFacts"), ("Goals", "MessageDirections"),
    ("CliBridge", "DeliversInput"), ("MessageDelivery", "DeliveredContext"),
    ("TeamUpdate", "TeamObservation"), ("BusReader", "MessageReferencesRead"),
):
    ta = re.sub(rf"\b{name}\b", replacement, ta)
ta = ta.replace("inputs: InputDispositions", "inputs: InputLookup[input_attempt.InputAttempt]")
for name in ("UserSourceTaskFact", "UserTaskCorrectionFact", "SubtaskTaskFact",
             "HumanConstraintTaskFact", "DecisionTaskFact", "ConstraintTaskFact"):
    old = f"        from agent_comms.retained_task_facts import {name}\n\n"
    if ta.count(old) != 1:
        raise ValueError(f"Original task constructor import changed: {name}")
    ta = ta.replace(old, "")
    ta = re.sub(rf"\b{name}\(", f"task_facts.{name}(", ta)

message_source = SRC / "messaging/messages.py"
mb = message_source.read_text()
old = '''        from agent_comms.retained_task_facts import ClaimTaskFact
        yield from super().retained_task_facts()
        yield ClaimTaskFact(ClaimMessage.of(self, self.claim_transition))'''
new = '''        yield from super().retained_task_facts()
        yield task_facts.ClaimTaskFact(ClaimMessage.of(self, self.claim_transition))'''
if mb.count(old) != 1:
    raise ValueError("Retain the exact original ClaimMessage.of constructor before composing")
ma = mb.replace(old, new).replace('''if TYPE_CHECKING:
    from agent_comms.bus_publication import Delivered
    from agent_comms.display_references import ReferenceContext
    from agent_comms.retained_task_facts import ExactTaskFact
''', "")
for name, replacement in (("Delivered", "bus_publication.Delivered"),
                          ("ReferenceContext", "display_references.ReferenceContext"),
                          ("ExactTaskFact", "task_facts.ExactTaskFact")):
    ma = re.sub(rf"\b{name}\b", replacement, ma)
ma = ma.replace("NO_HISTORY_SOURCE = NoHistorySource()",
                "NO_HISTORY_SOURCE = NoHistorySource()\n\n"
                "from agent_comms import bus_publication, display_references")

delivers = SRC / "delivers_input.py"
db = delivers.read_text()
da = db.replace("from agent_comms.messaging.messages import Message", "from agent_comms.messaging import messages")
da = re.sub(r"message: Message\b", "message: messages.Message", da)

publication = SRC / "bus_publication.py"
pb = publication.read_text()
publication_guard = '''if TYPE_CHECKING:
    from agent_comms.bus import Bus, BusReader
    from agent_comms.messaging.bus_record_builder import RecordBuilder
    from agent_comms.planning.goal_management import Goals
    from agent_comms.registry.registry_document import RegistrySnapshot
    from agent_comms.registry.threads import Thread
'''
if pb.count(publication_guard) != 1:
    raise ValueError("Original automated-publication guard changed")
pa = pb.replace(publication_guard, "")
for name, reference in (("BusReader", "MessageIdentityRead"), ("Bus", "MessageWriter"),
                        ("Goals", "MessageDirections"), ("RegistrySnapshot", "TaskRegistry[TaskOwner]"),
                        ("Thread", "TaskOwner")):
    pa = re.sub(rf"\b{name}\b", reference, pa)
pa = pa.replace("class Delivered(BusRecord, AudiencedMessage, declared_name=None):",
                "class Delivered(BusRecord, AudiencedMessage, MessageDeliveryRead, declared_name=None):")

changes = [
    {"operation": "create_file", "file_path": str(SRC / "task_callbacks.py"), "source": callbacks},
    patch(facts, [(fb, fa)]),
    patch(tasks, [(tb, ta)]),
    patch(message_source, [(mb, ma)]),
    patch(delivers, [(db, da)]),
    patch(SRC / "message_reads.py", [
        ("class MessageReferencesRead(ABC):", '''class MessageDeliveryRead(ABC):
    __slots__ = ()
    sender_lookup: ThreadLookup

    @property
    @abstractmethod
    def message(self) -> messages.Message: ...

    @abstractmethod
    def includes_lookup(self, lookup: ThreadLookup) -> bool: ...


class MessageReferencesRead(ABC):'''),
        ("    ) -> tuple[messages.Message, ...]: ...\n\n\nclass MessageIdentityRead", '''    ) -> tuple[messages.Message, ...]: ...

    @abstractmethod
    def deliveries_for_references(
        self, references: Iterable[MessageReference],
    ) -> tuple[MessageDeliveryRead, ...]: ...


class MessageIdentityRead'''),
    ]),
    patch(publication, [(pb, pa)]),
    patch(SRC / "context_segments/wake.py", [
        ("class MessageDelivery:", "class MessageDelivery(DeliveredContext):"),
        ("                from agent_comms.coordination.errors import IdentityConflict\n\n", ""),
    ]),
    patch(SRC / "task_attachments/team_pings.py", [
        ("class TeamUpdate(OwnerNotice, declared_name=None):",
         "class TeamUpdate(OwnerNotice, TeamObservation, declared_name=None):"),
    ]),
]
imports = [
    imported(facts, "from agent_comms import input_attempt"),
    imported(facts, "from agent_comms.messaging import messages"),
    imported(tasks, "from agent_comms import input_attempt"),
    imported(tasks, "from agent_comms import retained_task_facts as task_facts"),
    imported(tasks, "from agent_comms.messaging import messages"),
    imported(tasks, "from agent_comms.execution.delivers_input import DeliversInput"),
    imported(tasks, "from agent_comms.messaging.task_callbacks import DeliveredContext, TeamObservation"),
    imported(tasks, "from agent_comms.messaging.message_directions import MessageDirections"),
    imported(tasks, "from agent_comms.messaging.message_reads import MessageReferencesRead"),
    imported(tasks, "from agent_comms.execution.input_lookup import InputLookup"),
    imported(message_source, "from agent_comms import retained_task_facts as task_facts"),
    imported(SRC / "context_segments/wake.py", "from agent_comms.messaging.task_callbacks import DeliveredContext"),
    imported(SRC / "context_segments/wake.py", "from agent_comms.coordination.errors import IdentityConflict"),
    imported(SRC / "task_attachments/team_pings.py", "from agent_comms.messaging.task_callbacks import TeamObservation"),
    imported(publication, "from agent_comms.messaging.message_reads import MessageDeliveryRead, MessageIdentityRead"),
    imported(publication, "from agent_comms.messaging.message_writer import MessageWriter"),
    imported(publication, "from agent_comms.messaging.message_directions import MessageDirections"),
    imported(publication, "from agent_comms.messaging.bus_record_builder import RecordBuilder"),
    imported(publication, "from agent_comms.registry.task_owner import TaskOwner"),
    imported(publication, "from agent_comms.registry.task_registry import TaskRegistry"),
    {"operation": "remove_import_names", "file_path": str(publication),
     "module_name": "typing", "import_names": ["TYPE_CHECKING"]},
    {"operation": "remove_import_names", "file_path": str(message_source),
     "module_name": "typing", "import_names": ["TYPE_CHECKING"]},
]
print(json.dumps({"stages": [stage("p23-bind-original-task-fact-callbacks", reason, changes),
                              stage("p23-import-original-task-fact-owners", reason, imports)]}, indent=2))
