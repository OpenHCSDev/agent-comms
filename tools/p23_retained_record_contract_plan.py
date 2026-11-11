"""Match the live conversation capability to the original packed record provider."""

import json

from p23_revision_owner_plan import ROOT, SRC, patch, stage

reads = SRC / "message_reads.py"
before = reads.read_text()
conversation, remaining = before.split("class MessageDeliveryRead", 1)
if "class ConversationRead[Message](ABC):" not in conversation:
    raise ValueError("The live conversation callback declaration changed")
conversation = conversation.replace("ConversationRead[Message]", "ConversationRead[Record]")
conversation = conversation.replace("tuple[Message, ...]", "tuple[Record, ...]")
operations = [patch(reads, [(before, conversation + "class MessageDeliveryRead" + remaining)])]
facts = SRC / "retained_task_facts.py"
operations.append(patch(facts, [("source: ConversationRead[messages.Message]",
                                "source: ConversationRead[messages.BusRecord]")]))
tests = ROOT / "tests/test_retained_task_read_contracts.py"
before = tests.read_text()
after = before.replace("from agent_comms.messaging.messages import ClaimMessage, Message",
                       "from agent_comms.messaging.messages import BusRecord, ClaimMessage, Message")
after = after.replace('    assert get_type_hints(RetainedTaskFacts.capture)["source"] is ConversationRead',
'''    conversation = get_type_hints(RetainedTaskFacts.capture)["source"]
    assert get_origin(conversation) is ConversationRead
    assert get_args(conversation) == (BusRecord,)''')
after = after.replace('    assert get_type_hints(ConversationRead.conversation)["return"] == tuple[Message, ...]',
'''    assert issubclass(Delivered, BusRecord)''')
operations.append(patch(tests, [(before, after)]))
reason = (
    "The original BusReader.conversation return is tuple[Delivered, ...], not tuple[Message, ...]. "
    "RetainedTaskFacts.capture deliberately iterates each record.messages(), so replacing the record with one message would lose original packed facts. "
    "ConversationRead is a live covariant return capability over Record; the actual BusReader specializes it with the original Delivered, and capture accepts the original BusRecord base. "
    "Every original capture statement, concrete fact source field, wire constructor and publication body remains unchanged. "
    "Correct the unqualified new test's Message assumption to those actual record declarations; do not alter the product to satisfy the wrong assertion."
)
print(json.dumps({"stages": [
    stage("p23-retained-conversation-uses-original-packed-record-contract", reason, operations),
    stage("p23-actual-conversation-provider-returns-original-delivered-records", reason,
          [{"operation": "replace_class_base", "file_path": str(SRC / "bus.py"),
            "target_qualname": "BusReader", "base_name": "ConversationRead[Message]",
            "replacement_base_name": "ConversationRead[Delivered]"}]),
]}, indent=2))
