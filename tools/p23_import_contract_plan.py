"""Close original import-value and history callback dependencies without another decoder."""

import argparse
import json
from pathlib import Path

from pydantic import JsonValue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    def patch(file: str, pairs: list[tuple[str, str]], target: str | None = None) -> JsonValue:
        result: dict[str, JsonValue] = {
            "operation": "patch_target", "file_path": str(args.root / file),
            "replacements": [{"old_source": old, "new_source": new} for old, new in pairs],
        }
        if target is not None:
            result["target_qualname"] = target
        return result

    def stage(name: str, reason: str, operations: list[JsonValue]) -> JsonValue:
        return {"recipes": [{"recipe_id": name, "reason": reason, "operations": operations,
                              "architecture_guards": [], "authority_claims": []}],
                "architecture_guards": []}

    stages = [stage("p23-original-historical-position-and-import-role", (
        "ImportedConversation is a frozen value, not the importing algorithm. Its retained "
        "instruction field consumes the existing nominal historical-only branch, exactly as "
        "ImportedSessionMetadata already does; the actual Codex source keeps its identity, "
        "tag and fields. The existing ordering is (offset, instruction), not path order. "
        "Put that unchanged ordering answer on the actual source member, so the importer "
        "does not rediscover a concrete member by its fields. Change only the field contracts "
        "and the sorting callback, not tuple/set semantics, tie handling, records, defaults "
        "or key order. The native-entry guarded annotation was already unbound at runtime; "
        "delete its guard now and bind the same original value from its new owner after the "
        "next atomic stage. Intermediate stages need not be imported; final source must be "
        "cold-tested. No opaque source can enter the nominal historical branch by shape."
    ), [
        patch("src/agent_comms/execution/turn_context.py", [
            ('    """A retained instruction from a source session, never current execution authority."""',
             '    """A retained instruction from a source session, never current execution authority."""\n\n    @abstractmethod\n    def instruction_position(self) -> tuple[int, int]:\n        """Its original record and instruction ordering coordinates."""\n        ...'),
        ], "HistoricalInstructionProvenance"),
        patch("src/agent_comms/cli_bridges/codex_records.py", [
            ("    def public_description(self) -> str:",
             "    def instruction_position(self) -> tuple[int, int]:\n        return self.offset, self.instruction\n\n    def public_description(self) -> str:"),
        ], "CodexRolloutProvenance"),
        patch("src/agent_comms/importing.py", [
            ("from agent_comms.cli_bridges.codex_records import CodexRolloutProvenance",
             "from agent_comms.execution.turn_context import HistoricalInstructionProvenance"),
        ]),
        patch("src/agent_comms/importing.py", [
            ("historical_instructions: tuple[CodexRolloutProvenance, ...]", "historical_instructions: tuple[HistoricalInstructionProvenance, ...]"),
        ], "ImportedConversation"),
        patch("src/agent_comms/importing.py", [
            ("historical_instructions: tuple[CodexRolloutProvenance, ...]", "historical_instructions: tuple[HistoricalInstructionProvenance, ...]"),
        ], "ThreadImported"),
        patch("src/agent_comms/importing.py", [
            ("instructions: set[CodexRolloutProvenance]", "instructions: set[HistoricalInstructionProvenance]"),
            ("key=lambda value: (value.offset, value.instruction)", "key=lambda value: value.instruction_position()"),
        ], "ImportBuffer"),
        patch("src/agent_comms/execution/native_entries.py", [
            ("from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, Never, Self, TextIO, cast",
             "from typing import Annotated, ClassVar, Literal, Never, Self, TextIO, cast"),
            ("if TYPE_CHECKING:\n    from agent_comms.importing import ImportedConversation\n\n", ""),
        ]),
    ])]
    stages.append(stage("p23-lower-original-imported-conversation-values", (
        "Move the exact original ImportedConversation and KeptPiSession together below the "
        "native writer and importing implementation. Keep frozen/slots/kw-only options, "
        "Harness identity, title/defaults, counts, the abstract session_bytes contract and "
        "KeptPiSession's byte-for-byte read unchanged. Migrate every indexed consumer and "
        "preserve actual imports still needed by ImportSnapshot and ThreadImported. No "
        "wrapper, cloned data model, renamed wire field, carry or compatibility loader."
    ), [{"operation": "extract_symbols_to_new_module",
         "file_path": str(args.root / "src/agent_comms/importing.py"),
         "symbol_qualnames": ["ImportedConversation", "KeptPiSession"],
         "destination_path": str(args.root / "src/agent_comms/imported_conversation.py"),
         "destination_source": '"""Original saved-conversation values shared by import and native publication."""\n\nfrom __future__ import annotations\n'}]))
    stages.append(stage("p23-native-writer-original-value-binding", (
        "ImportedSessionWriter.preparing now names the exact original lower value at runtime. "
        "Only its annotation binding changes; private-directory preparation, AtomicFile "
        "publication, external source read, context-manager lifetime and cancellation remain."
    ), [{"operation": "ensure_import",
         "file_path": str(args.root / "src/agent_comms/execution/native_entries.py"),
         "import_source": "from agent_comms.execution.imported_conversation import ImportedConversation"}]))
    stages.append(stage("p23-compose-existing-import-history-capabilities", (
        "ImportSnapshot.read consumes only a history stream and its existing harness identity. "
        "Compose the original ReadsHistory and HarnessSession as one unnamed nominal parent; "
        "actual History inherits it. It has no fields, constructor, forwarding method or "
        "second registry. Keep the same family root, concrete reader names, crossing harness/"
        "offline/online membership, callbacks and discovery. Preserve both portable snapshot "
        "capture and Pi's separately overridden byte-preserving import; no factory method is "
        "merged or removed. Positive MRO and real all-harness tests are still required."
    ), [{"operation": "create_file", "file_path": str(args.root / "src/agent_comms/import_history_source.py"),
         "source": '"""The existing stream and harness capabilities consumed by portable import."""\n\nfrom agent_comms.agent_runtime import ReadsHistory\nfrom agent_comms.harness_install import HarnessSession\n\n\nclass ImportHistorySource(ReadsHistory, HarnessSession, declared_name=None):\n    """An original harness session that can supply its retained transcript updates."""\n'},
        patch("src/agent_comms/cli_bridges/history.py", [
            ("from agent_comms.agent_runtime import ReadsHistory\n", ""),
            ("from agent_comms.importing import", "from agent_comms.execution.import_history_source import ImportHistorySource\nfrom agent_comms.importing import"),
            ("class History(ReadsHistory, HarnessSession, declared_name=None):", "class History(ImportHistorySource, declared_name=None):"),
        ]),
    ]))
    stages.append(stage("p23-import-factory-original-runtime-bindings", (
        "With original values below the native writer and actual History on its composed "
        "contract, ImportSnapshot.read can name that contract and the existing native writer "
        "at runtime. Hoist only its exact original local imports and guarded value types. "
        "Preserve asyncio.run/collection, buffer lifetime, budgets, compaction, retained-source "
        "ordering and generated Pi bytes. No local replacement, async wrapper, hint cache "
        "or decoder copy. The native values move removes the previously demonstrated cycle."
    ), [
        patch("src/agent_comms/importing.py", [
            ("from typing import TYPE_CHECKING\n", ""),
            ("if TYPE_CHECKING:\n    from agent_comms.cli_bridges.history import History\n    from agent_comms.runtime_updates import SessionFacts\n    from agent_comms.transcript_events import TranscriptEvent\n\n", ""),
            ("from agent_comms.execution.turn_item import NativeItemId",
             "from agent_comms.execution.turn_item import NativeItemId\nfrom agent_comms.execution.native_entries import ImportedSessionWriter, NativeEntry, SessionEntry\nfrom agent_comms.execution.import_history_source import ImportHistorySource\nfrom agent_comms.runtime_updates import SessionFacts\nfrom agent_comms.transcript_events import TranscriptEvent"),
            ("def read(cls, history: History, limits: ImportLimits)", "def read(cls, history: ImportHistorySource, limits: ImportLimits)"),
            ("        from agent_comms.execution.native_entries import (\n            ImportedSessionWriter,\n            NativeEntry,\n            SessionEntry,\n        )\n\n", ""),
        ]),
    ]))
    stages.append(stage("p23-existing-runtime-capability-values-at-runtime", (
        "The native source import graph found no reverse path from Outcome, Message or "
        "RuntimeUpdate to agent_runtime. Use those exact original declarations for its "
        "existing capability methods at runtime, without a generic stand-in or repeated "
        "abstract signature. No runtime implementation or controller is imported. Keep "
        "every base, method, coroutine/result shape and capability membership unchanged. "
        "Cold entry and original consumer tests must check actual import/class side effects; "
        "the graph alone is not behavior proof."
    ), [patch("src/agent_comms/agent_runtime.py", [
        ("from typing import TYPE_CHECKING\n", ""),
        ("if TYPE_CHECKING:\n    from agent_comms.harness_delivery_states import Outcome\n    from agent_comms.messages import Message\n    from agent_comms.runtime_updates import RuntimeUpdate",
         "from agent_comms.harness_delivery_states import Outcome\nfrom agent_comms.messages import Message\nfrom agent_comms.runtime_updates import RuntimeUpdate"),
        ("This module is light: runtimes import it, it imports no runtime.",
         "Runtimes implement these capabilities; callbacks name their shared value types directly."),
    ])]))
    args.plan.write_text(json.dumps({"stages": stages}, indent=2) + "\n")
    print(json.dumps({"stages": len(stages)}))


if __name__ == "__main__":
    main()
