"""Bind original callback/author modules after their original declarations are available."""

import json

from p23_revision_owner_plan import SRC, patch, stage

operations = []
path = SRC / "bus_record_builder.py"
before = path.read_text()
statement = "from agent_comms.messaging import messages\n"
if statement not in before:
    raise ValueError("Original RecordBuilder message binding changed")
after = before.replace(statement, "").rstrip() + "\n\n" + statement
operations.append(patch(path, [(before, after)]))

path = SRC / "message_writer.py"
before = path.read_text()
after = before.replace("from agent_comms.messaging.bus_record_builder import RecordBuilder", "")
after = after.replace(statement, "")
after = after.replace("build: RecordBuilder", "build: bus_record_builder.RecordBuilder")
after = after.rstrip() + '''

import agent_comms.messaging.bus_record_builder as bus_record_builder
from agent_comms.messaging import messages
'''
operations.append(patch(path, [(before, after)]))

path = SRC / "initial_origin.py"
before = path.read_text()
after = before.replace("from agent_comms.messaging.bus_record_builder import RecordBuilder", "")
after = after.replace("from agent_comms.messaging.message_writer import MessageWriter", "")
after = after.replace(statement, "")
after = after.replace("build: RecordBuilder", "build: bus_record_builder.RecordBuilder")
after = after.replace("bus: MessageWriter", "bus: message_writer.MessageWriter")
after = after.replace(": MessageDirections", ": MessageDirections[messages.Message]")
after = after.rstrip() + '''

import agent_comms.messaging.bus_record_builder as bus_record_builder
import agent_comms.message_writer as message_writer
from agent_comms.messaging import messages
'''
operations.append(patch(path, [(before, after)]))

path = SRC / "input_origin.py"
before = path.read_text()
old = "from agent_comms.messaging.initial_origin import HumanOrigin"
if old not in before:
    raise ValueError("Original HumanOrigin author import changed")
after = before.replace(old, "")
after = after.replace("author: HumanOrigin", "author: initial_origin.HumanOrigin")
after = after.replace("author=HumanOrigin.of(user)", "author=initial_origin.HumanOrigin.of(user)")
after = after.replace("import agent_comms.input_attempt as input_attempt",
                      "from agent_comms import initial_origin\nimport agent_comms.input_attempt as input_attempt")
operations.append(patch(path, [(before, after)]))

reason = (
    "RecordBuilder is an original PEP-695 lazy alias, and MessageWriter/InitialOrigin use the original record classes only in postponed method annotations or unchanged method bodies. "
    "Bind their ordinary original modules after the alias and class declarations exist, so an independently imported callback module does not request a callback/author class before that class is declared. "
    "HumanInputOrigin.author remains the original concrete initial_origin.HumanOrigin; qualification changes no wire field, default, constructor argument, acquisition or check order. "
    "Normal imports still execute modules; this cut relies on each module exposing its required declarations before starting that execution, never on an import guard, local import, deferred loader or namespace cache. "
    "RecordBuilder keeps exactly its original concrete Callable result, and all append, publish and human-input procedures retain their original bodies except the proven same-original-module name qualification."
)
print(json.dumps({"stages": [stage("p23-original-origin-callback-module-declaration-order", reason, operations)]}, indent=2))
