"""Keep P23's stronger identity/creation contract on the original P34 read provider."""

import argparse
import ast
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    path = Path.cwd() / "src/agent_comms/registry/thread_identity_source.py"
    before = path.read_text()
    after = before.replace("from abc import abstractmethod\n", "")
    after = after.replace(
        "from agent_comms.registry.thread_creation import ThreadCreationSource\n",
        "from agent_comms.registry.thread_creation import ThreadCreationSource\n"
        "from agent_comms.registry.thread_incarnation_read import ReadsThreadIncarnation\n",
    )
    after = after.replace(
        "class ThreadIdentitySource(ThreadCreationSource):",
        "class ThreadIdentitySource(ThreadCreationSource, ReadsThreadIncarnation[ThreadIncarnation]):",
    )
    duplicate = "    @property\n    @abstractmethod\n    def incarnation(self) -> ThreadIncarnation: ...\n"
    if duplicate not in after:
        raise ValueError("The stronger source no longer owns the expected duplicate property")
    after = after.replace(duplicate, "")
    ast.parse(after, filename=str(path))
    args.output.write_text(json.dumps({"recipes": [{
        "recipe_id": "p23-original-identity-provider-composition",
        "reason": "P23 ThreadIdentitySource requires name, original CreationTime and incarnation; P34 ReadsThreadIncarnation requires only incarnation. Preserve the stronger declaration, fields, slots, ThreadCreationSource parent and TaggedThreadSource/ThreadProvenance composition. Inherit the original narrower provider specialized by actual ThreadIncarnation, deleting the repeated abstract property and its unused decorator import. No record, constructor, method body, permission order or competing direct base changes.",
        "operations": [{"operation": "patch_target", "file_path": str(path),
                        "replacements": [{"old_source": before, "new_source": after}]}],
    }]}, indent=2) + "\n")


if __name__ == "__main__":
    main()
