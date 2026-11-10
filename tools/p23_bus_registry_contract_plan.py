"""Keep original claim/namespace callbacks below registry snapshot assembly."""

import argparse
import ast
import json
import re
from pathlib import Path


def bodies(source: str) -> list[str]:
    return [ast.dump(node.body, include_attributes=False) if isinstance(node.body, ast.AST)
            else repr([ast.dump(statement, include_attributes=False) for statement in node.body])
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path.cwd()
    bus = root / "src/agent_comms/messaging/bus.py"
    claims = root / "src/agent_comms/messaging/envelope_claim_transitions.py"
    snapshot = root / "src/agent_comms/registry/registry_document.py"
    bus_before = bus.read_text()
    bus_after = bus_before.replace(
        "from agent_comms.registry import registry_document\n", "",
    ).replace(
        "from agent_comms.errors import CommittedMessageError, RelationViolationError, UnclaimedError\n",
        "from agent_comms.errors import CommittedMessageError, RelationViolationError, UnclaimedError\n"
        "from agent_comms.messaging.claim_registry_read import ClaimRegistryRead\n"
        "from agent_comms.registry.presence_reads import PresenceThread\n"
        "from agent_comms.registry.registry_names import RegistryNames\n"
        "from agent_comms.registry.thread_identity_source import ThreadIdentitySource\n",
    ).replace("snapshot: registry_document.RegistrySnapshot", "snapshot: RegistryNames[ThreadIdentitySource]")
    bus_after = bus_after.replace("registry: registry_document.RegistrySnapshot", "registry: ClaimRegistryRead[Owner]")
    bus_after = bus_after.replace("def prepare_claim[Change: ClaimTransition](",
        "def prepare_claim[Change: ClaimTransition, Owner: PresenceThread](")
    bus_after = bus_after.replace("def acquire_resource_claim(",
        "def acquire_resource_claim[Owner: PresenceThread](")
    claim_before = claims.read_text()
    claim_after = re.sub(r"def (apply|acquire|live|live_in)\(",
        r"def \1[Owner: PresenceThread](", claim_before)
    claim_after = claim_after.replace("registry: RegistrySnapshot", "registry: ClaimRegistryRead[Owner]")
    claim_after = claim_after.replace("from __future__ import annotations\n",
        "from __future__ import annotations\n\n"
        "from agent_comms.messaging.claim_registry_read import ClaimRegistryRead\n"
        "from agent_comms.registry.presence_reads import PresenceThread\n")
    patches = []
    for path, before, after in ((bus, bus_before, bus_after), (claims, claim_before, claim_after)):
        if before == after or bodies(before) != bodies(after):
            raise ValueError(f"Callback change did not preserve original procedures: {path}")
        ast.parse(after, filename=str(path))
        patches.append({"operation": "patch_target", "file_path": str(path),
                        "replacements": [{"old_source": before, "new_source": after}]})
    source = ('"""Original acquired-registry callbacks used to validate resource claims."""\n\n'
              'from abc import ABC, abstractmethod\n'
              'from collections.abc import Iterable, Iterator\n\n'
              'from agent_comms.registry.presence_reads import PresenceThread\n'
              'from agent_comms.registry.thread_identity import RecordedThreadIncarnation\n\n\n'
              'class ClaimRegistryRead[Owner: PresenceThread](ABC):\n'
              '    __slots__ = ()\n\n'
              '    @abstractmethod\n'
              '    def claim_generation(self, name: str) -> int: ...\n\n'
              '    @abstractmethod\n'
              '    def threads_for(self, incarnation: RecordedThreadIncarnation) -> tuple[Owner, ...]: ...\n\n'
              '    @abstractmethod\n'
              '    def active_threads(self, candidates: Iterable[Owner]) -> Iterator[Owner]: ...\n')
    reason = "The current paired startup reaches low Bus's registry_document import, used only by three live method annotations. Answer selection reads canonical names/names_for from the existing RegistryNames provider. Claim liveness uses exactly claim_generation, threads_for and active_threads from the actual acquired RegistrySnapshot and name/process_alive from its actual Thread (already PresenceThread). Declare ClaimRegistryRead and have the original RegistrySnapshot inherit it specialized by actual Thread. Quantify only live method callback owner types; retain every original stored field, constructor, method body, active-member order, generation/process check, transaction and snapshot. No surrogate registry, generic stored record, forwarding method, second acquisition or fallback. Author verifies all affected procedure bodies AST-identical."
    args.output.write_text(json.dumps({"stages": [
        {"recipes": [{"recipe_id": "p23-original-claim-registry-contract", "reason": reason,
                      "operations": [{"operation": "create_file",
                          "file_path": str(root / "src/agent_comms/messaging/claim_registry_read.py"), "source": source}]}]},
        {"recipes": [{"recipe_id": "p23-live-registry-callback-types", "reason": reason,
                      "operations": [*patches, {"operation": "ensure_import", "file_path": str(snapshot),
                          "import_source": "from agent_comms.messaging.claim_registry_read import ClaimRegistryRead"}]}]},
        {"recipes": [{"recipe_id": "p23-actual-claim-registry-provider", "reason": reason,
                      "operations": [{"operation": "add_class_base", "file_path": str(snapshot),
                          "target_qualname": "RegistrySnapshot", "base_name": "ClaimRegistryRead[Thread]"},
                          {"operation": "remove_import_bindings", "file_path": str(claims),
                           "bound_names": ["RegistrySnapshot"]}]}]},
    ]}, indent=2) + "\n")


if __name__ == "__main__":
    main()
