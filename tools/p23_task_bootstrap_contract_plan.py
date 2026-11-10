"""Keep live read/delivery callbacks generic without changing any persisted source field."""

import json

from p23_revision_owner_plan import SRC, patch, stage

operations = []
for filename, changes in (
    ("delivers_input.py", [
        ("from agent_comms.messaging import messages\n", ""),
        ("class DeliversInput(ABC):", "class DeliversInput[Message](ABC):"),
        ("messages.Message", "Message"),
    ]),
    ("message_directions.py", [
        ("from agent_comms.messaging import messages\n", ""),
        ("class MessageDirections(ABC):", "class MessageDirections[Message](ABC):"),
        ("messages.Message", "Message"),
    ]),
    ("message_reads.py", [
        ("class ConversationRead(ABC):", "class ConversationRead[Message](ABC):"),
        ("class MessageDeliveryRead(ABC):", "class MessageDeliveryRead[Message](ABC):"),
        ("class MessageReferencesRead(ABC):", "class MessageReferencesRead[Message](ABC):"),
        ("messages.Message", "Message"),
        ("tuple[MessageDeliveryRead, ...]", "tuple[MessageDeliveryRead[Message], ...]"),
        ("import agent_comms.messaging.messages as messages\n", ""),
    ]),
):
    path = SRC / filename
    before = path.read_text()
    after = before
    for old, new in changes:
        if old not in after:
            raise ValueError(f"Original callback binding changed in {filename}: {old}")
        after = after.replace(old, new)
    operations.append(patch(path, [(before, after)]))

path = SRC / "task_sources.py"
before = path.read_text()
after = before
for statement in (
    "from agent_comms import input_attempt, retained_task_facts as task_facts\n",
    "from agent_comms.messaging import messages\n",
):
    if statement not in after:
        raise ValueError(f"Original task module binding changed: {statement}")
    after = after.replace(statement, "")
for callback in ("DeliversInput", "MessageDirections", "MessageReferencesRead"):
    after = after.replace(f": {callback}", f": {callback}[messages.Message]")
after = after.rstrip() + '''

from agent_comms import input_attempt, retained_task_facts as task_facts
from agent_comms.messaging import messages
'''
operations.append(patch(path, [(before, after)]))
for filename, old, new in (
    ("retained_task_facts.py", ": ConversationRead", ": ConversationRead[messages.Message]"),
    ("messaging/messages.py", "tuple[MessageDeliveryRead, ...]", "tuple[MessageDeliveryRead[Message], ...]"),
    ("bus_publication.py", ": MessageDirections", ": MessageDirections[Message]"),
):
    path = SRC / filename
    before = path.read_text()
    if old not in before:
        raise ValueError(f"Original callback use changed in {filename}: {old}")
    operations.append(patch(path, [(before, before.replace(old, new))]))

reason = (
    "The original task-root control fails because task_sources imports input_attempt before defining NoTaskAttachment, then input_origin/initial_origin/RecordBuilder import messages, which needs that original attachment declaration. "
    "The nominal callback modules also import messages while task_sources is still bootstrapping. "
    "Parameterize only the state-free live callback APIs and specialize their actual providers with the original Message class; no persisted record/fact/source field becomes generic. "
    "Keep task_sources' ordinary input/fact/message module bindings after its original declarations; their uses are postponed annotations or unchanged method bodies, not class bases or eager defaults. "
    "The concrete UserSourceTaskFact/AuthoredTaskFact/ClaimTaskFact/InputTaskFact fields remain the original messages.Message/messages.ClaimMessage/input_attempt.StoredInput and are not replaced. "
    "All constructors, defaults, task order, publication methods and runtime placement remain unchanged."
)
providers = []
for filename, target, base in (
    ("bus.py", "BusReader", "ConversationRead"),
    ("bus.py", "BusReader", "MessageReferencesRead"),
    ("bus_publication.py", "Delivered", "MessageDeliveryRead"),
    ("planning/goal_management.py", "Goals", "MessageDirections"),
    ("cli_bridges/shared.py", "CliBridge", "DeliversInput"),
):
    providers.append({"operation": "replace_class_base", "file_path": str(SRC / filename),
                      "target_qualname": target, "base_name": base,
                      "replacement_base_name": f"{base}[Message]"})
print(json.dumps({"stages": [stage("p23-live-task-callbacks-without-message-bootstrap-imports", reason, operations),
                              stage("p23-actual-task-providers-retain-original-message-specialization", reason,
                                    providers[:1] + providers[2:]),
                              stage("p23-remaining-busreader-reference-specialization", reason,
                                    providers[1:2])]}, indent=2))
