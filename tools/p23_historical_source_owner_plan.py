"""Lower original imported metadata, then relocate its exact authenticated Codex source."""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--previous-plan", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    previous = json.loads(args.previous_plan.read_text())
    metadata_cut = {
        "recipes": [{
            "recipe_id": "p23-original-historical-instruction-contract",
            "reason": (
                "NRA's actual import graph gives both cli_protocol and runtime_updates -> "
                "external_transcript -> execution.native_transcript -> execution.native_entries "
                "-> importing. Native entries need the original ImportedSessionMetadata, not "
                "the importer algorithm. Give retained historical instruction sources a nominal "
                "branch of the existing Provenance family, with the actual Codex provider "
                "inheriting it, rather than admitting every Provenance or adding a type/string "
                "switch. The metadata's source field names that historical-only contract. "
                "CodexRolloutProvenance retains its distinct identity, exact fields and tag. "
                "There is no other current provider of this capability. Preserve the durable "
                "metadata shape, Harness format name and all selected-file/incarnation checks. "
                "Qualify unchanged Codex metadata bytes, native entry decoding, historical "
                "source reads with no Comms, and refusal of unrelated provenance members. "
                "This is a declaration-level dependency repair, not a compatibility codec."
            ),
            "operations": [{
                "operation": "patch_target",
                "file_path": str(args.root / "src/agent_comms/execution/turn_context.py"),
                "replacements": [{
                    "old_source": 'class Provenance(DeclaredFamily, affix="Provenance"):',
                    "new_source": 'class Provenance(DeclaredFamily, affix="Provenance", discovery_package="agent_comms.cli_bridges.codex_records"):',
                }, {
                    "old_source": '@dataclass(frozen=True)\nclass CodexRolloutProvenance(Provenance):',
                    "new_source": 'class HistoricalInstructionProvenance(Provenance, declared_name=None):\n    """A retained instruction from a source session, never current execution authority."""\n\n\n@dataclass(frozen=True)\nclass CodexRolloutProvenance(HistoricalInstructionProvenance):',
                }],
            }, {
                "operation": "patch_target",
                "file_path": str(args.root / "src/agent_comms/importing.py"),
                "replacements": [{
                    "old_source": "from agent_comms.execution.turn_context import CodexRolloutProvenance",
                    "new_source": "from agent_comms.execution.turn_context import CodexRolloutProvenance, HistoricalInstructionProvenance",
                }, {
                    "old_source": '    historical_instructions: tuple[CodexRolloutProvenance, ...] = ()\n\n    @classmethod\n    def sources_for_owner',
                    "new_source": '    historical_instructions: tuple[HistoricalInstructionProvenance, ...] = ()\n\n    @classmethod\n    def sources_for_owner',
                }],
            }],
            "architecture_guards": [], "authority_claims": [],
        }], "architecture_guards": [],
    }
    lower_metadata = {
        "recipes": [{
            "recipe_id": "p23-lower-original-imported-session-metadata",
            "reason": (
                "Move the exact original frozen slotted ImportedSessionMetadata below the "
                "native entry reader and importer. Its source membership, borrowed registry "
                "and selected-file checks stay on that same declaration. Rewrite every indexed "
                "consumer to its owner; retain only an ordinary import used by the importer, "
                "not a compatibility export. No field mapping, copy, adapter or loader is added. "
                "The preceding historical-source contract keeps the lower record independent "
                "of the concrete Codex decoder without dropping its nominal admission rule."
            ),
            "operations": [{
                "operation": "extract_symbols_to_new_module",
                "file_path": str(args.root / "src/agent_comms/importing.py"),
                "symbol_qualnames": ["ImportedSessionMetadata"],
                "destination_path": str(args.root / "src/agent_comms/imported_session_metadata.py"),
                "destination_source": '"""The original saved import observation, shared by its reader and writer."""\n\nfrom __future__ import annotations\n',
            }],
            "architecture_guards": [], "authority_claims": [],
        }], "architecture_guards": [],
    }
    # The complete, atomically refused earlier plan is retained as evidence. Reuse its
    # exact model selection and relocation after cutting the demonstrated metadata edge.
    model_move, _, provenance_move, finish = previous["stages"]
    remove_reader_binding = {
        "recipes": [{
            "recipe_id": "p23-remove-old-reader-provenance-binding-before-relocation",
            "reason": "The reader's real source construction imports the same declaration from its new owner after relocation; no source-module alias remains.",
            "operations": [{
                "operation": "remove_import_names",
                "file_path": str(args.root / "src/agent_comms/cli_bridges/codex_rollout.py"),
                "module_name": "agent_comms.execution.turn_context",
                "import_names": ["CodexRolloutProvenance"],
            }], "architecture_guards": [], "authority_claims": [],
        }], "architecture_guards": [],
    }
    stages = [metadata_cut, lower_metadata, model_move, remove_reader_binding, provenance_move, finish]
    args.plan.write_text(json.dumps({"stages": stages}, indent=2) + "\n")
    print(json.dumps({"stages": len(stages)}))


if __name__ == "__main__":
    main()
