"""Bind input observations to original evidence and namespace contracts, not live controllers."""

import argparse
import ast
import json
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_modules
from pydantic import JsonValue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    path = args.root / "src/agent_comms/input_attempt.py"
    module, = parse_python_modules(path, parse_workers=1)
    operations: list[JsonValue] = [{
        "operation": "patch_target", "file_path": str(path), "replacements": [
            {"old_source": "from typing import TYPE_CHECKING, Annotated, ClassVar, Self", "new_source": "from typing import Annotated, ClassVar, Self"},
            {"old_source": "if TYPE_CHECKING:\n    from agent_comms.execution.turn_lease import TurnLeaseFence\n    from agent_comms.registry.registry_document import RegistrySnapshot\n\n", "new_source": ""},
            {"old_source": "from agent_comms.registry.thread_identity import GenerationCounter, ThreadIncarnation",
             "new_source": "from agent_comms.registry.thread_identity import GenerationCounter, ThreadIncarnation, TurnFence\nfrom agent_comms.registry.registry_names import RegistryNames\nfrom agent_comms.registry.thread_identity_source import ThreadIdentitySource"},
        ],
    }]
    selected = []
    for owner in module.syntax_index.instances_of(ast.ClassDef):
        assert owner in module.module.body, "Nested/conditional input providers need a separate ownership decision"
        for method in owner.body:
            match method:
                case ast.FunctionDef(args=parameters):
                    replacements: list[JsonValue] = []
                    for parameter in (*parameters.posonlyargs, *parameters.args, *parameters.kwonlyargs):
                        match parameter:
                            case ast.arg(arg="lease", annotation=ast.Name(id="TurnLeaseFence")):
                                replacements.append({"old_source": "lease: TurnLeaseFence", "new_source": "lease: TurnFence"})
                            case ast.arg(arg="snapshot", annotation=ast.Name(id="RegistrySnapshot")):
                                replacements.append({"old_source": "snapshot: RegistrySnapshot", "new_source": "snapshot: RegistryNames[ThreadIdentitySource]"})
                    if replacements:
                        target = f"{owner.name}.{method.name}"
                        operations.append({"operation": "patch_target", "file_path": str(path),
                                           "target_qualname": target, "replacements": replacements})
                        selected.append(target)
    assert selected
    test = '''def test_input_observation_callbacks_use_original_evidence_contracts() -> None:
    for state in (InputAttempt, *InputAttempt.members_with(InputAttempt)):
        for callback in (state.context_committed, state.started_for_native, state.delivered_in):
            hints = get_type_hints(callback)
            if "lease" in hints:
                assert hints["lease"] is TurnFence
                assert get_origin(hints["snapshot"]) is RegistryNames
                assert get_args(hints["snapshot"]) == (ThreadIdentitySource,)


'''
    operations.append({
        "operation": "patch_target", "file_path": str(args.root / "tests/test_goal_runtime_annotations.py"),
        "replacements": [
            {"old_source": "from agent_comms.input_attempt import StoredInput", "new_source": "from agent_comms.input_attempt import InputAttempt, StoredInput"},
            {"old_source": "def test_declared_policy_keys_keep_their_original_message_and_identity_sources() -> None:",
             "new_source": test + "def test_declared_policy_keys_keep_their_original_message_and_identity_sources() -> None:"},
        ],
    })
    reason = (
        "InputAttempt's methods report durable evidence, never execution/replay authority. "
        "The complete native class/method parameter census selects only its original guarded "
        "lease and snapshot annotations. StartedInput.matches_lease reads exactly identity."
        "incarnation, claimed_generation and turn_id; these already belong to TurnFence, "
        "and actual TurnLeaseFence inherits it. The incarnation's matches_recorded_name/"
        "resolved/current methods only consume original RegistryNames canonical aliases and "
        "threads whose real provider has an incarnation. Use existing RegistryNames specialized "
        "to ThreadIdentitySource, not RegistrySnapshot's activity/controller assembly. Preserve "
        "all original method bodies, name/birth/alias comparisons, native IDs, text digest, "
        "generation checks, unknown/cleared transitions and immutable native context proof. "
        "Do not add a role, wrapper, cached snapshot, new data field, alias or helper. This is "
        "not a grant to write or start input: owned_send.py:219-232 still checks the original "
        "owner and obtains require_turn_lease; InputDispositions.context_committed at 473-490 "
        "still independently checks the current complete lease and claim generation before "
        "any document transition. That producer and its signature are unchanged. The lower "
        "observational contract matches the already documented inability of a stored row to "
        "invent the missing birth/process proof. Native syntax application does not prove "
        "authority preservation: retain the source-stashed missing-name failure and rerun "
        "original context, lease, mismatch, cancellation, delivery and input document cases. "
        "One family-level test asks every real input provider's public callbacks for runtime "
        "hints; it imports only names that exist on the old source so its control collects."
    )
    args.plan.write_text(json.dumps({"recipes": [{"recipe_id": "p23-original-input-observation-contracts",
                                                "reason": reason, "operations": operations,
                                                "architecture_guards": [], "authority_claims": []}],
                                     "architecture_guards": []}, indent=2) + "\n")
    print(json.dumps({"callbacks": selected, "operations": len(operations)}))


if __name__ == "__main__":
    main()
