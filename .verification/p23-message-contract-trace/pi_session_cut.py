"""Author the original Pi child/session cut through native NRA operations only."""
from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path

from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodSourceSnapshot

ARTIFACTS = Path(__file__).resolve().parent
ROOT = ARTIFACTS.parents[1]
REVISION = "622c26d425e2482d3b430e4c747ebcdeb83a5e31"


def main() -> None:
    archive = subprocess.check_output(
        ["git", "archive", REVISION, "src", "tests", "tools"], cwd=ROOT,
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        sources = {
            str(ROOT / member.name): tree.extractfile(member).read().decode()
            for member in tree.getmembers()
            if member.isfile() and member.name.endswith(".py")
        }
    snapshot = CodemodSourceSnapshot.from_source_mapping(
        sources, analysis_roots=(ROOT / "src", ROOT / "tests", ROOT / "tools"),
    )
    child = str(ROOT / "src/agent_comms/execution/native_child.py")
    backend = str(ROOT / "src/agent_comms/pi_native_backend.py")
    retention = "NativeSessionRetention[PiSessionChild, NativeChildState, NativeSessionIdentity]"
    cuts = [
        {
            "operation": "patch_target", "file_path": child,
            "target_qualname": method,
            "replacements": [{
                "old_source": "persistent: PiNativeBackend",
                "new_source": "persistent: " + retention,
            }],
        }
        for method in (
            "NativeChildState.inspect", "RetiringNative.inspect",
            "BorrowedNative.inspect", "RetainedNative.inspect_acquired",
        )
    ]
    cuts.append({
        "operation": "patch_target", "file_path": child,
        "replacements": [{
            "old_source": "    from agent_comms.pi_native_backend import PiNativeBackend\n",
            "new_source": "",
        }],
    })
    imported = {
        "operation": "ensure_import", "file_path": child,
        "import_source": "from agent_comms.execution.session_retention import NativeSessionRetention",
    }
    extraction = {
        "operation": "extract_symbols_to_new_module", "file_path": backend,
        "symbol_qualnames": ["PersistentPiSession"],
        "destination_path": str(ROOT / "src/agent_comms/execution/persistent_pi_session.py"),
    }
    stages = [
        {"recipes": [{
            "recipe_id": "p23-native-child-uses-original-retention-contract",
            "reason": "The four child inspection annotations require only the original retention parent's lock and child_state, not the whole registered backend. No concrete wire field or method body changes. This is a live provider annotation, not generic wire decoding.",
            "operations": [*cuts, imported],
        }]},
        {"recipes": [{
            "recipe_id": "p23-original-persistent-session-below-registered-backend",
            "reason": "Move the original concrete PersistentPiSession unchanged below the registered backend; native extraction owns dependency and consumer rewriting. Keep its original child states and acquisition order. No vocabulary member promotion or construction-hook bypass.",
            "operations": [extraction],
        }]},
    ]
    plan = {"stages": stages}
    target = ARTIFACTS / "pi-session-622.plan.json"
    target.write_text(json.dumps(plan, indent=2) + "\n")
    print(f"Core {REVISION}; {len(sources)} pinned source/test/tool inputs; plan {target}", flush=True)
    prefix = {"stages": stages[:1]}
    (ARTIFACTS / "pi-session-622.prefix.plan.json").write_text(json.dumps(prefix, indent=2) + "\n")
    prefix_simulation = CodemodPlanRoot.from_json_value(prefix).as_sequence().simulate(snapshot)
    (ARTIFACTS / "pi-session-622.prefix.simulation.json").write_text(
        json.dumps(prefix_simulation.execution_payload(), indent=2) + "\n",
    )
    print(f"Native annotation-cut prefix clean={prefix_simulation.is_clean}", flush=True)
    simulation = CodemodPlanRoot.from_json_value(plan).as_sequence().simulate(snapshot)
    (ARTIFACTS / "pi-session-622.simulation.json").write_text(
        json.dumps(simulation.execution_payload(), indent=2) + "\n",
    )
    print(f"Native result clean={simulation.is_clean}; stages={simulation.stage_count}", flush=True)


if __name__ == "__main__":
    main()
