"""Bind actual callback-only classes after their module's original declarations exist."""

import json
import re

from p23_revision_owner_plan import SRC, patch, stage

boundaries = (
    ("bus.py", '''if TYPE_CHECKING:
    from agent_comms.messaging.display_unread_selection import DisplayUnreadSelection
    from agent_comms.registry.registry_document import RegistrySnapshot
''', {"DisplayUnreadSelection": "read_selection.DisplayUnreadSelection",
        "RegistrySnapshot": "registry_document.RegistrySnapshot"},
     "from agent_comms.messaging import read_selection\nfrom agent_comms.registry import registry_document\n"),
    ("publisher.py", '''if TYPE_CHECKING:
    from agent_comms.coordination.session import NativeCapture
    from agent_comms.registry.threads import Thread
''', {"NativeCapture": "coordination_session.NativeCapture", "Thread": "threads.Thread"},
     "from agent_comms.coordination import session as coordination_session\nfrom agent_comms.registry import threads\n"),
    ("registry/registration.py", '''if TYPE_CHECKING:
    from agent_comms.agent_backend import RuntimeLocation
    from agent_comms.catalog_document import CatalogDocument
    from agent_comms.execution.selected.participant import SelectedParticipant
    from agent_comms.planning.goal_management import Goals
    from agent_comms.planning.goals import GoalDeclaration
''', {"RuntimeLocation": "agent_backend.RuntimeLocation", "CatalogDocument": "catalog_document.CatalogDocument",
        "SelectedParticipant": "selected_participant.SelectedParticipant", "Goals": "goal_management.Goals",
        "GoalDeclaration": "goals.GoalDeclaration"},
     "from agent_comms import agent_backend, catalog_document\n"
     "from agent_comms.execution.selected import participant as selected_participant\n"
     "from agent_comms.planning import goal_management, goals\n"),
)
operations = []
for relative, guard, names, imports in boundaries:
    path = SRC / relative
    before = path.read_text()
    if before.count(guard) != 1:
        raise ValueError(f"Original callback-only guard changed: {relative}")
    after = before.replace(guard, "")
    for name, reference in names.items():
        after = re.sub(rf"\b{name}\b", reference, after)
    after = after.rstrip() + "\n\n" + imports
    operations.append(patch(path, [(before, after)]))
reason = (
    "The bounded complete-name traces show these guarded classes used only by postponed public/nested callback annotations (plus the RuntimeLocation docstring). "
    "Bind their actual modules unconditionally after this module's original declarations, then spell each annotation through that module. "
    "The original class identities, callback types, fields, signatures, constructors and bodies do not change; no import is function-local or conditional and no namespace lookup cache is introduced. "
    "Completing declarations before reciprocal annotation-module acquisition addresses the actual eager definition boundary; runtime annotations and every cold root still require qualification. "
    "This authored exact-source spelling step is bounded to these traced uses, not a generic lexical parser or a claim for other guarded names."
)
cleanup = [{"operation": "remove_import_names", "file_path": str(SRC / relative),
            "module_name": "typing", "import_names": ["TYPE_CHECKING"]}
           for relative, _, _, _ in boundaries]
print(json.dumps({"stages": [
    stage("p23-bind-original-callback-types-after-declarations", reason, operations),
    stage("p23-remove-unused-callback-guard-imports", reason, cleanup),
]}, indent=2))
