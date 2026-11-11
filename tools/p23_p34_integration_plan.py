"""Resolve P34's original owners without undoing P23's already-lowered declarations."""

import json

from p23_revision_owner_plan import ROOT, SRC, imported, patch, stage

imports = []
for relative in ("src/agent_comms/cli_bridges/history.py", "src/agent_comms/cli_bridges/pi_session.py",
                 "src/agent_comms/registry/thread_management.py", "tests/test_bulk_lifetimes.py",
                 "tests/test_importing.py"):
    path = ROOT / relative
    imports.extend((
        {"operation": "remove_import_names", "file_path": str(path),
         "module_name": "agent_comms.importing", "import_names": ["ImportLimits"]},
        imported(path, "from agent_comms.execution.imported_messages import ImportLimits"),
    ))
for relative, names in (("src/agent_comms/importing.py", ["ImportedInput", "ImportedMessage", "NoRetainedRequest"]),
                        ("tests/test_importing.py", ["ImportedMessage", "ImportedReply", "ImportedRequest"])):
    path = ROOT / relative
    imports.extend((
        {"operation": "remove_import_names", "file_path": str(path),
         "module_name": "agent_comms.transcript_events", "import_names": names},
        imported(path, "from agent_comms.imported_messages import " + ", ".join(names)),
    ))
imports.append(imported(SRC / "importing.py", "from agent_comms.execution.imported_messages import ImportLimits"))
removed = '''@dataclass(frozen=True, slots=True)
class ImportLimits:
    messages: int = 200
    characters: int = 120_000
    per_message: int = 12_000

    def __post_init__(self) -> None:
        if min(self.messages, self.characters, self.per_message) <= 0:
            raise ValueError("Import budgets must be positive.")
'''
reason = (
    "Integrate original P34 commit 6e5cb83d5 while retaining P23's actual ImportedConversation, ImportedSessionMetadata and ImportHistorySource owners. "
    "The six Git conflicts are overlapping old import/class locations, not competing source semantics. "
    "Delete the old ImportLimits declaration, use agent_comms.imported_messages for the original imported-message family and limits, "
    "and make ImportBuffer.append consume retained_request().present() exactly as P34 does, before bounding the message. "
    "No re-export, remember callback, duplicate class, budget change, wire-field change or guarded-import restoration."
)
print(json.dumps({"stages": [
    stage("p23-p34-delete-original-import-limit-copy-and-reverse-callback", reason, [
        patch(SRC / "importing.py", [
            (removed, ""),
            ("        message.remember(self)",
             "        for request in message.retained_request().present():\n"
             "            self.latest_request = request.body[: self.limits.per_message]"),
        ]),
    ]),
    stage("p23-p34-use-both-original-owner-cuts", reason, imports),
]}, indent=2))
