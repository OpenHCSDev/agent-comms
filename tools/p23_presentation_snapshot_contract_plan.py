"""Keep concrete goal snapshots above actions; put their read capability below both."""

import json

from p23_revision_owner_plan import ROOT, SRC, imported, patch, stage

snapshots = SRC / "goal_snapshots.py"
controls = SRC / "planning/goal_presentation.py"
goals = SRC / "planning/goal_management.py"
presentation = SRC / "presentation.py"
test = ROOT / "tests/test_revision_owner_annotations.py"
capability = '''"""The execution read from an owning goal snapshot, without its action controls."""

from abc import ABC

from agent_comms.planning.goal_execution import GoalExecution


class GoalExecutionSnapshot(ABC):
    __slots__ = ()

    execution: GoalExecution
'''
old_source = snapshots.read_text()
new_source = old_source.replace(
    "from agent_comms.planning.goal_presentation import GoalSnapshot\n", "",
).replace(
    "from agent_comms.registry.threads import Thread\n", "",
).replace("class GoalSnapshots(ABC):", "class GoalSnapshots[Owner, Snapshot: GoalExecutionSnapshot](ABC):").replace(
    "self, owner: Thread,", "self, owner: Owner,",
).replace(
    ") -> GoalSnapshot:", ") -> Snapshot:",
)
presentation_before = presentation.read_text()
presentation_after = presentation_before.replace(
    ": GoalSnapshots", ": GoalSnapshots[Thread, GoalExecutionSnapshot]",
)
test_before = test.read_text()
test_after = test_before.replace(
    "from typing import get_type_hints", "from typing import get_args, get_origin, get_type_hints",
).replace(
    "from agent_comms.planning.goal_management import Goals",
    "from agent_comms.planning.goal_management import Goals\n"
    "from agent_comms.planning.goal_presentation import GoalExecutionControls, GoalSnapshot\n"
    "from agent_comms.registry.threads import Thread",
).replace(
    '    goal_source = get_type_hints(BusPresentation.__init__)["goals"]',
    '    goal_alias = get_type_hints(BusPresentation.__init__)["goals"]\n'
    '    goal_source = get_origin(goal_alias)\n'
    '    owner, snapshot_contract = get_args(goal_alias)',
).replace(
    "    assert get_type_hints(goal_source.snapshot_for) == get_type_hints(Goals.snapshot_for)",
    "    assert owner is Thread\n"
    "    assert snapshot_contract in GoalSnapshot.__bases__\n"
    '    assert get_type_hints(GoalSnapshot)["execution"] is GoalExecutionControls\n'
    "    owning_alias, = (base for base in Goals.__orig_bases__\n"
    "                      if get_origin(base) is goal_source)\n"
    "    assert get_args(owning_alias) == (Thread, GoalSnapshot)\n"
    "    owner_parameter, snapshot_parameter = goal_source.__type_params__\n"
    '    assert get_type_hints(goal_source.snapshot_for)["owner"] is owner_parameter\n'
    '    assert get_type_hints(goal_source.snapshot_for)["return"] is snapshot_parameter',
)
print(json.dumps({"stages": [stage(
    "p23-retain-concrete-goal-snapshot-with-lower-read-capability",
    "The retained cold failure proves GoalSnapshot imports action infrastructure through goal_presentation -> goal_actions -> goal_records -> message_bus -> historical_views -> presentation. The consumer requires only snapshot.execution, not controls or concrete snapshot construction. Add a state-free nominal execution-read capability to the actual GoalSnapshot; retain its exact module, frozen dataclass, fields and concrete GoalExecutionControls annotation. Parameterize the provider contract by its original owner and concrete result, as existing GoalFork contracts do; this is not generic wire decoding. Goals binds Thread/GoalSnapshot, presentation reads Thread/GoalExecutionSnapshot. No record is replaced, persisted, wrapped or weakened. Native C3, concrete-field annotation, original callbacks, source-hidden control and real consumers remain proof obligations.",
    [
        {"operation": "create_file", "file_path": str(SRC / "goal_execution_snapshot.py"), "source": capability},
        patch(snapshots, [(old_source, new_source)]),
        imported(snapshots, "from agent_comms.planning.goal_execution_snapshot import GoalExecutionSnapshot"),
        imported(controls, "from agent_comms.planning.goal_execution_snapshot import GoalExecutionSnapshot"),
        {"operation": "remove_import_names", "file_path": str(controls), "module_name": "typing", "import_names": ["TYPE_CHECKING"]},
        patch(controls, [("class GoalSnapshot(GoalPublicationSnapshot):", "class GoalSnapshot(GoalPublicationSnapshot, GoalExecutionSnapshot):")]),
        patch(goals, [("GoalReadyGrantSource[NativeThread, Generation], GoalSnapshots,", "GoalReadyGrantSource[NativeThread, Generation], GoalSnapshots[Thread, GoalSnapshot],")]),
        patch(presentation, [(presentation_before, presentation_after)]),
        imported(presentation, "from agent_comms.planning.goal_execution_snapshot import GoalExecutionSnapshot"),
        patch(test, [(test_before, test_after)]),
    ],
)]}, indent=2))
