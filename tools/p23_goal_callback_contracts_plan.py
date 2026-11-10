"""Cut live goal callback imports while retaining all concrete provider and wire types."""

import argparse
import ast
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--toad", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    patches = []

    def patch(path: Path, replacements: tuple[tuple[str, str], ...]) -> None:
        before = path.read_text()
        after = before
        for old, new in replacements:
            if old not in after:
                raise ValueError(f"Missing determining callback annotation in {path}: {old}")
            after = after.replace(old, new)
        ast.parse(after, filename=str(path))
        patches.append({"operation": "patch_target", "file_path": str(path),
                        "replacements": [{"old_source": before, "new_source": after}]})

    patch(root / "src/agent_comms/planning/goal_control.py", (
        ("class GoalControlSource[Authority, Action](ABC):",
         "class GoalControlSource[Authority, Action, Subject, Declaration](ABC):"),
        ("-> GoalDeclaration", "-> Declaration"),
        ("goal: Goal", "goal: Subject"),
    ))
    for path, owner in (
        (root / "src/agent_comms/planning/goal_actions.py", "Authority"),
        (root / "tests/test_goal_control_source.py", "object"),
        (args.toad / "src/toad/acp/goal_control.py", "OwnerRequest"),
    ):
        patch(path, ((f"GoalControlSource[{owner}, GoalAction[GoalDeclaration]]",
                      f"GoalControlSource[{owner}, GoalAction[GoalDeclaration], Goal, GoalDeclaration]"),))
    patch(root / "src/agent_comms/planning/goal_snapshots.py", (
        ("class GoalSnapshots[Owner, Snapshot: GoalExecutionSnapshot](ABC):",
         "class GoalSnapshots[Owner, Snapshot: GoalExecutionSnapshot, Registry](ABC):"),
        ("snapshot: RegistrySnapshot", "snapshot: Registry"),
    ))
    patch(root / "src/agent_comms/presentation.py", ((
        "GoalSnapshots[Thread, GoalExecutionSnapshot]",
        "GoalSnapshots[Thread, GoalExecutionSnapshot, RegistrySnapshot]",
    ),))
    patch(root / "src/agent_comms/planning/goal_management.py", ((
        "GoalSnapshots[Thread, GoalSnapshot]", "GoalSnapshots[Thread, GoalSnapshot, RegistrySnapshot]",
    ),))
    reason = "GoalControlSource and GoalSnapshots are live abstract callbacks, never stored records. Quantify their subject/declaration/registry arguments and specialize every original consumer/provider with the same concrete Goal, GoalDeclaration and RegistrySnapshot. Keep every field, method body, constructor, acquisition and authorization order unchanged. Move the whole original GoalFailureOwner capability out of the goal assignment read surface so only its actual users import its concrete Checkout/ProcessState requirements. No surrogate record, forwarding service, guard promotion or local import."
    args.output.write_text(json.dumps({"stages": [
        {"recipes": [{"recipe_id": "p23-goal-live-callback-arguments", "reason": reason,
                      "operations": patches}]},
        {"recipes": [{"recipe_id": "p23-goal-delete-unused-concrete-callback-imports", "reason": reason,
                      "operations": [
                          {"operation": "remove_import_bindings", "file_path": str(root / "src/agent_comms/planning/goal_control.py"),
                           "bound_names": ["Goal", "GoalDeclaration"]},
                          {"operation": "remove_import_bindings", "file_path": str(root / "src/agent_comms/planning/goal_snapshots.py"),
                           "bound_names": ["RegistrySnapshot"]},
                      ]}]},
        {"recipes": [{"recipe_id": "p23-original-goal-failure-owner", "reason": reason,
                      "operations": [{"operation": "relocate_symbols_to_new_module",
                          "file_path": str(root / "src/agent_comms/planning/goal_work.py"),
                          "destination_path": str(root / "src/agent_comms/planning/goal_failure_owner.py"),
                          "symbol_qualnames": ["GoalFailureOwner"]}]}]},
    ]}, indent=2) + "\n")


if __name__ == "__main__":
    main()
