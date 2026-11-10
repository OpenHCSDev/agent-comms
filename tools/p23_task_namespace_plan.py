"""Prepare the original namespace/read-role cut as an NRA transaction."""

import argparse
import json
from pathlib import Path

from pydantic import JsonValue


def patch(path: Path, replacements: list[dict[str, str]], target: str = "") -> dict[str, JsonValue]:
    operation = {"operation": "patch_target", "file_path": str(path),
                 "replacements": replacements}
    if target:
        operation["target_qualname"] = target
    return operation


def recipe(name: str, reason: str, operations: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    return {"recipes": [{"recipe_id": name, "reason": reason,
                         "operations": operations, "architecture_guards": [],
                         "authority_claims": []}], "architecture_guards": []}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    source = args.root / "src/agent_comms"
    tasks = source / "task_sources.py"
    before = tasks.read_text()
    after = before.replace(
        "from agent_comms.registry.registry_document import RegistrySnapshot",
        "from agent_comms.registry.task_registry import TaskRegistry",
    ).replace(
        "from agent_comms.registry_provenance import RegistryProvenance",
        "from agent_comms.registry.registry_names import RegistryNames",
    ).replace(
        "from agent_comms.registry.threads import Thread\n",
        "from agent_comms.registry.threads import Thread, ThreadProvenance\n",
    ).replace("RegistrySnapshot", "TaskRegistry").replace(
        "RegistryProvenance", "RegistryNames[ThreadProvenance]",
    )
    assert before != after
    stages = [recipe(
        "p23-lower-original-namespace-declarations",
        "The cold task root reaches Message through RegistrySnapshot -> AgentActivity -> "
        "Coordination -> Bus, and through RegistryProvenance's activity dependency. "
        "The original RegistryNames implementation and DeletedThread value do not depend "
        "on that live activity owner. Move those exact declarations with their existing "
        "Member TypeVar below both snapshot and recorded-provenance assembly. Preserve "
        "every resolution, alias, retirement, refusal and field operation. NRA migrates "
        "indexed consumers, including tests. The source keeps RegistryNames for real "
        "inheritance/capture uses; unused compatibility-only bindings are removed next. "
        "This is dependency repair, not relocation sold as reduced implementation facts.",
        [{"operation": "extract_symbols_to_new_module",
          "file_path": str(source / "registry_provenance.py"),
          "symbol_qualnames": ["Member", "DeletedThread", "RegistryNames"],
          "destination_path": str(source / "registry_names.py"),
          "destination_source": '"""Original namespace resolution, below live activity and snapshot assembly."""\n\nfrom __future__ import annotations\n'}],
    ), recipe(
        "p23-compose-original-task-namespace-read-role",
        "Task declarations use the original namespace operations plus claim_generation. "
        "Move that existing abstract operation below RegistryPresence as TaskRegistry, "
        "composing the original RegistryNames[Thread]. RegistryPresence inherits the role; "
        "RegistryDocument and RegistrySnapshot keep their exact concrete generation reads "
        "and all existing fields. No store, generation, status, adapter or forwarding "
        "implementation is added. Preserve slots and metaclass behavior. Keep annotation "
        "evaluation unchanged during movement, then explicitly enable Python 3.14 native "
        "lazy annotations on the new namespace owner to retain require_member's lexical "
        "Kind parameter; runtime annotation checks must qualify that separate change. The task callbacks "
        "name the inherited role and the original RegistryNames instead of importing whole "
        "live snapshot/provenance assembly backwards. Cold imports, actual identity/refusal "
        "paths and original sender-binding bytes must qualify this authored decision.",
        [{"operation": "create_file", "file_path": str(source / "task_registry.py"),
          "source": '"""The original live namespace reads consumed by authored task declarations."""\n\nfrom abc import abstractmethod\n\nfrom agent_comms.registry.registry_names import RegistryNames\nfrom agent_comms.registry.threads import Thread\n\n\nclass TaskRegistry(RegistryNames[Thread]):\n    __slots__ = ()\n\n    @abstractmethod\n    def claim_generation(self, name: str) -> int: ...\n'},
         patch(source / "registry/registry_document.py", [
             {"old_source": "class RegistryPresence[StatusMap: Mapping[str, ThreadStatus]](RegistryNames[Thread]):",
              "new_source": "class RegistryPresence[StatusMap: Mapping[str, ThreadStatus]](TaskRegistry):"},
             {"old_source": "    @abstractmethod\n    def claim_generation(self, name: str) -> int: ...\n",
              "new_source": ""},
         ]),
         {"operation": "ensure_import", "file_path": str(source / "registry/registry_document.py"),
          "import_source": "from agent_comms.registry.task_registry import TaskRegistry"},
         patch(tasks, [{"old_source": before, "new_source": after}]),
         {"operation": "remove_import_names", "file_path": str(source / "registry_provenance.py"),
          "module_name": ".registry_names", "import_names": ["Member", "DeletedThread"]},
         {"operation": "remove_import_names", "file_path": str(source / "registry_names.py"),
          "module_name": "__future__", "import_names": ["annotations"]},
         {"operation": "remove_import_names", "file_path": str(source / "registry/registry_document.py"),
          "module_name": "..registry_names", "import_names": ["RegistryNames"]},
        ],
    )]
    args.plan.write_text(json.dumps({"stages": stages}, indent=2) + "\n")
    print(json.dumps({"stages": len(stages), "task_namespace_references":
                      before.count("RegistrySnapshot") + before.count("RegistryProvenance")}))


if __name__ == "__main__":
    main()
