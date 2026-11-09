"""Author and simulate P23's remaining provider plan; never apply product edits."""
from __future__ import annotations

import ast
import difflib
import json
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_module_roots
from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodSourceSnapshot

ARTIFACTS = Path(__file__).resolve().parent
ROOT = ARTIFACTS.parents[1]
SRC = ROOT / "src/agent_comms"

DISPLAY = '''"""The source operations display choices consume, independent of their services."""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from typing import Self


class SentClocks(ABC):
    __slots__ = ()
    sent: Mapping[str, float]


class ActivitySource(ABC):
    __slots__ = ()

    @abstractmethod
    def opened_activity(self) -> AbstractContextManager[SentClocks]: ...


class SentHistorySource[Registry](ABC):
    __slots__ = ()
    bus: ActivitySource

    @abstractmethod
    def last_sent_timestamps(
        self, rows: Iterable[tuple[str, float]], *, registry: Registry,
    ) -> dict[str, float]: ...


class StoredThreadOrder[Order](ABC):
    __slots__ = ()
    order: Order


class ChannelOrderSource[ThreadOrder, ChannelOrder](ABC):
    __slots__ = ()

    @abstractmethod
    def set_channel_sort(self, name: str, order: ThreadOrder) -> StoredThreadOrder[ThreadOrder]: ...

    @abstractmethod
    def set_channel_order(self, order: ChannelOrder) -> ChannelOrder: ...


class SortedChannel[View, Order](ABC):
    __slots__ = ()
    name: str
    created_at: float

    @abstractmethod
    def sort_key(self, order: Order, view: View) -> tuple[int, int, float, float, str]: ...


class ChannelSortView[Order](ABC):
    __slots__ = ()
    channel: SortedChannel[Self, Order]
    last_activity: float
    last_user_input: float
'''

GOAL_WORK = '''"""The assignment read surface consumed by a goal, without a dependency on its stores."""

from abc import ABC, abstractmethod

from agent_comms.execution.child_process import ProcessState
from agent_comms.nominal.field_codec import GoalId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import ObservedTurn, ThreadIncarnation


class GoalObservation(ABC):
    __slots__ = ()

    @property
    @abstractmethod
    def captured(self) -> object: ...

    @abstractmethod
    def accepts_observation(self, goal_id: GoalId, minimum_revision: int) -> bool: ...


class GoalOwner(ABC):
    __slots__ = ()
    name: str
    goal: GoalObservation

    @property
    @abstractmethod
    def incarnation(self) -> ThreadIncarnation: ...

    @abstractmethod
    def require_goal_assignment(self, actor: ThreadIncarnation) -> None: ...


class GoalFailureOwner(GoalOwner):
    __slots__ = ()
    worktree: Checkout
    process_identity: ProcessState

    @abstractmethod
    def observed_turn(self, claim_scope: int) -> ObservedTurn: ...


class GoalAssignment(ABC):
    __slots__ = ()

    @abstractmethod
    def goal_instructions(self) -> tuple[str, ...]: ...

    @abstractmethod
    def progress_line(self) -> str: ...


class GoalAssignments(ABC):
    __slots__ = ()

    @abstractmethod
    def for_goal(self, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[GoalAssignment, ...]: ...

    @abstractmethod
    def closed_for_goal(self, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[GoalAssignment, ...]: ...

    @abstractmethod
    def holds_pending(self, owner: ThreadIncarnation) -> bool: ...


class GoalWorkSource(ABC):
    @property
    @abstractmethod
    def todos(self) -> GoalAssignments: ...
'''

RELATIONSHIP = '''class RelationshipSource(SentHistorySource[RegistrySnapshot]):
    __slots__ = ()
    root: Path
    registry: Registration
    bus: BusResources
    channels: RelationshipCatalogSource

    @abstractmethod
    def revision(self) -> presentation.WireRevision: ...

    @abstractmethod
    def thread_views(
        self, visibility: RosterVisibility,
    ) -> tuple[presentation.ThreadView, ...]: ...'''

def main() -> int:
    print("Reading current source through NRA; no apply path exists in this runner.", flush=True)
    selected = {"display_sources.py", "goal_work.py", "relationships.py", "activity_clocks.py",
                "messaging/published_bus.py", "history_views.py", "channel_management.py",
                "messaging/channels.py", "presentation.py", "registry/threads.py",
                "planning/goals.py", "planning/orders.py", "goal_ledger.py",
                "planning/goal_attempt_phase.py", "planning/goal_failure_observation.py"}
    dependencies = {"display_order.py", "catalog_store.py", "catalog_document.py", "bus.py",
                    "bus_activity_index.py", "messaging/message_bus.py", "registry/registry_document.py",
                    "registry/registration.py", "registry_provenance.py", "registry/thread_management.py",
                    "registry/thread_identity.py", "registry/thread_creation.py", "context_owner.py",
                    "task_owner.py", "thread_identity_source.py", "class_fact.py", "tool_result.py",
                    "nominal/declared_family.py", "storage/typed_table.py", "goal_mutation.py",
                    "execution/turn_context.py", "planning/goal_records.py", "planning/goal_management.py",
                    "planning/goal_states.py", "planning/goal_generation.py", "planning/goal_attempts.py",
                    "order_story.py", "lifecycle_capability.py", "messaging/messages.py", "task_sources.py",
                    "retained_task_facts.py", "input_attempt.py", "input_origin.py"}
    roots = tuple(SRC / relative for relative in sorted(selected | dependencies))
    modules = parse_python_module_roots((*roots, ROOT / "tests/test_goal_runtime_annotations.py"))
    snapshot = CodemodSourceSnapshot.from_modules(modules)
    before = snapshot.sources_by_file_path
    operations: list[dict[str, object]] = []
    patches: dict[str, list[tuple[str, str]]] = {}

    def path(relative: str) -> str:
        return str(SRC / relative)

    def old(relative: str) -> str:
        return before[path(relative)]

    def declaration(relative: str, qualname: str) -> str:
        module = snapshot.parsed_module_for_source_path(path(relative))
        owner = module.module
        for part in qualname.split("."):
            owner, = (node for node in module.syntax_index.named_scope_children[owner]
                      if node.name == part and node in owner.body)
        start = min(owner.lineno, *(node.lineno for node in owner.decorator_list)) if owner.decorator_list else owner.lineno
        return "".join(old(relative).splitlines(keepends=True)[start - 1:owner.end_lineno]).rstrip("\n")

    def patch(relative: str, pairs: list[tuple[str, str]]) -> None:
        patches.setdefault(relative, []).extend(pairs)

    def imported(relative: str, statement: str) -> None:
        operations.append({"operation": "ensure_import", "file_path": path(relative), "import_source": statement})

    patch("display_sources.py", [(old("display_sources.py"), DISPLAY)])
    patch("goal_work.py", [(old("goal_work.py"), GOAL_WORK)])
    for relative, role in (("planning/goal_attempt_phase.py", "GoalAttemptTransition"),
                           ("planning/goal_failure_observation.py", "GenerationFailureSource[Failure]")):
        stem = role.split("[")[0]
        original = declaration(relative, stem)
        replacement = original.replace("(Protocol):", "(ABC):", 1)
        replacement = replacement.replace("\n    def ", "\n    __slots__ = ()\n\n    @abstractmethod\n    def ", 1)
        imports = (("from typing import Protocol\n", "") if relative.endswith("goal_failure_observation.py")
                   else ("from typing import ClassVar, Protocol", "from typing import ClassVar"))
        patch(relative, [imports, (original, replacement)])

    imported("activity_clocks.py", "from agent_comms.display_sources import SentClocks")
    patch("activity_clocks.py", [("class ActivityClocks:", "class ActivityClocks(SentClocks):")])
    imported("messaging/published_bus.py", "from agent_comms.display_sources import ActivitySource")
    patch("messaging/published_bus.py", [("class BusResources:", "class BusResources(ActivitySource):")])
    imported("channel_management.py", "from agent_comms.display_sources import ChannelOrderSource")
    patch("channel_management.py", [("class ChannelManagement:", "class ChannelManagement(ChannelOrderSource[type[ThreadSort], type[ChannelSort]]):")])
    imported("messaging/channels.py", "from agent_comms.display_sources import ChannelSortView, SortedChannel, StoredThreadOrder")
    patch("messaging/channels.py", [
        ("from agent_comms.display_order import CreatedThreadSort, ThreadSort", "from agent_comms.display_order import ChannelSort, CreatedThreadSort, ThreadSort"),
        ("class Channel(DeclaredFamily, CliResult, ToolResult, ProjectionSource, affix=\"Channel\"):",
         "class Channel(DeclaredFamily, CliResult, ToolResult, ProjectionSource, StoredThreadOrder[type[ThreadSort]], SortedChannel[ChannelSortView[type[ChannelSort]], type[ChannelSort]], affix=\"Channel\"):")])
    imported("presentation.py", "from agent_comms.display_sources import ChannelSortView")
    patch("presentation.py", [("class ChannelView(ToolResult):", "class ChannelView(ToolResult, ChannelSortView[type[ChannelSort]]):")])

    # Reuse BusResources. The catalog move remains in the retained refused plan.
    imported("relationships.py", "from agent_comms.messaging.published_bus import BusResources")
    patch("relationships.py", [
        ("from agent_comms.bus import BusReader\n", ""),
        ("from agent_comms.display_sources import ActivitySource, SentHistorySource", "from agent_comms.display_sources import SentHistorySource"),
        (declaration("relationships.py", "RelationshipBusSource") + "\n\n\n", ""),
        (declaration("relationships.py", "RelationshipSource"), RELATIONSHIP)])
    imported("history_views.py", "from agent_comms.relationships import RelationshipSource")
    patch("history_views.py", [("class HistoryViews:", "class HistoryViews(RelationshipSource):")])

    imported("registry/threads.py", "from agent_comms.goal_work import GoalFailureOwner")
    patch("registry/threads.py", [("CatalogSelection, DeclaredFamily, TaskOwner,", "CatalogSelection, DeclaredFamily, TaskOwner, GoalFailureOwner,")])
    patch("planning/goals.py", [
        ("from typing import TYPE_CHECKING, Annotated, ClassVar, Literal", "from typing import Annotated, ClassVar, Literal"),
        ("from agent_comms.goal_work import GoalAssignment, GoalOwner, GoalWorkSource", "from agent_comms.goal_work import GoalAssignment, GoalObservation, GoalOwner, GoalWorkSource"),
        ("RegistrationInheritance[\"GoalDeclaration\"],\n", "RegistrationInheritance[\"GoalDeclaration\"], GoalObservation,\n"),
        ('return "\\n\\n".join(todo.order.instructions(todo) for todo in todos if todo.pending)',
         'return "\\n\\n".join(text for todo in todos for text in todo.goal_instructions())')])
    imported("planning/orders.py", "from agent_comms.goal_work import GoalAssignment, GoalAssignments")
    patch("planning/orders.py", [
        ("class Todo(TodoStoreRows, TypedTable):", "class Todo(TodoStoreRows, TypedTable, GoalAssignment):"),
        ("class TodoStore(OwnerRequestStore, TodoHistory):", "class TodoStore(OwnerRequestStore, TodoHistory, GoalAssignments):"),
        ("    def progress_line(self) -> str:\n", '''    def goal_instructions(self) -> tuple[str, ...]:
        """The original pending order contributes one instruction, every other order none."""
        return tuple(item.order.instructions(item)
                     for item in self.order.items_with(Pending, self))

    def progress_line(self) -> str:
''')])
    imported("goal_ledger.py", "from agent_comms.planning.goal_attempt_phase import GoalAttemptTransition")
    imported("goal_ledger.py", "from agent_comms.planning.goal_failure_observation import GenerationFailureSource")
    patch("goal_ledger.py", [
        ("class Generation(GoalLedgerTable, TypedTable, GoalGenerationRead):", "class Generation(GoalLedgerTable, TypedTable, GoalGenerationRead, GenerationFailureSource[\"FailedTurnEvidence\"]):"),
        ("class AttemptRecord(GoalLedgerTable, TypedTable):", "class AttemptRecord(GoalLedgerTable, TypedTable, GoalAttemptTransition):")])

    reason = ("P23 admitted actual-provider membership from the retained 18-provider source map. "
              "Replace structural contracts by state-free nominal ABCs, preserving original fields and provider bodies. "
              "Delete RelationshipBusSource in favor of existing BusResources; leave the original catalog contract and its provider pair unchanged behind the retained native extraction refusal. "
              "Delete the overpromising GoalAssignmentOrder: Todo owns zero-or-one instructions selected through its original Pending capability, "
              "and the goal joins those in original todo order. FoldedProblemOrder stays unchanged. "
              "These exact authored patches establish source changes, not metaclass/import/behavior equivalence. "
              "The original guarded/local imports and cold initialization obligations are recorded separately; no apply or execution proof is claimed.")
    source_patches = [{"operation": "patch_target", "file_path": path(relative),
                       "replacements": [{"old_source": left, "new_source": right}
                                        for left, right in pairs]}
                      for relative, pairs in patches.items()]
    def stage(identity: str, items: list[dict[str, object]]) -> dict[str, object]:
        return {"recipes": [{"recipe_id": identity, "reason": reason, "operations": items,
                             "architecture_guards": [], "authority_claims": []}], "architecture_guards": []}

    plan = {"stages": [stage("p23-remaining-provider-imports", operations),
                       stage("p23-remaining-provider-membership-and-owner-api", source_patches)]}
    operation_count = len(operations) + len(source_patches)
    (ARTIFACTS / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    census = []
    for relative in sorted(selected):
        module = snapshot.parsed_module_for_source_path(path(relative))
        census.extend({"path": relative, "line": node.lineno, "name": node.name,
                       "direct": node in module.module.body}
                      for _, node in module.syntax_index.indexed_nodes_of_type(ast.ClassDef))
    (ARTIFACTS / "class-census.json").write_text(json.dumps(census, indent=2) + "\n")
    print(f"Simulating {operation_count} operations on {len(before)} indexed source files.", flush=True)
    sequence = CodemodPlanRoot.from_json_value(plan).as_sequence()
    simulation = sequence.simulate(snapshot)
    (ARTIFACTS / "simulation.json").write_text(json.dumps(simulation.execution_payload(), indent=2) + "\n")
    after = simulation.final_snapshot.sources_by_file_path
    changed = sorted(p for p in after if before.get(p) != after[p])
    combined = "".join(line for p in changed for line in difflib.unified_diff(
        before.get(p, "").splitlines(keepends=True), after[p].splitlines(keepends=True),
        fromfile=str(Path(p).relative_to(ROOT)), tofile=str(Path(p).relative_to(ROOT))))
    (ARTIFACTS / "projected.diff").write_text(combined)
    print(f"Native simulation report: {ARTIFACTS / 'simulation.json'}", flush=True)
    return 0 if simulation.is_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
