"""Finish moved import consumers through existing authority and original callback values."""

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

    runtime_test = '''def test_original_import_factory_callbacks_use_actual_source_capabilities() -> None:
    source = get_type_hints(ImportSnapshot.read)["history"]
    assert issubclass(History, source)
    assert issubclass(source, ReadsHistory)
    assert issubclass(source, HarnessSession)
    assert get_type_hints(source.history)["return"] == AsyncIterator[RuntimeUpdate]
    original_value = get_type_hints(ImportedSessionWriter.preparing)["snapshot"]
    assert issubclass(ImportSnapshot, original_value)
    assert get_type_hints(ImportBuffer.describe)["facts"] is SessionFacts
    assert get_args(get_type_hints(ImportBuffer.add)["events"]) == (TranscriptEvent,)
    assert get_type_hints(PublishesTranscript.transcript)["return"] == AsyncIterator[RuntimeUpdate]
    delivery = get_type_hints(DeliversInput.deliver)
    assert delivery["message"] is Message
    assert delivery["return"] == type[Outcome]


'''
    operations = [
        patch("src/agent_comms/cli_bridges/history.py", [
            ("from typing import TYPE_CHECKING, BinaryIO, ClassVar, Self", "from typing import BinaryIO, ClassVar, Self"),
            ("if TYPE_CHECKING:\n    from agent_comms.cli_bridges.opencode_parts import SavedMessage\n    from agent_comms.cli_bridges.opencode_protocol import Session",
             "from agent_comms.cli_bridges.opencode_parts import SavedMessage\nfrom agent_comms.cli_bridges.opencode_protocol import Session"),
        ]),
        patch("src/agent_comms/registry/thread_management.py", [
            ("    TYPE_CHECKING,\n", ""),
            ("if TYPE_CHECKING:\n    from agent_comms.cli_bridges.history import History\n    from agent_comms.registry.registry_store import RegistryEdit\n    from agent_comms.tool_caller import ThreadCaller",
             "from agent_comms.cli_bridges.history import History\nfrom agent_comms.registry.registry_store import RegistryEdit\nfrom agent_comms.read_basis import ReadAuthority"),
        ]),
        patch("src/agent_comms/registry/thread_management.py", [
            ("caller: ThreadCaller", "caller: ReadAuthority"),
            ("        with self.registry.store.editing() as edit:\n", "        with self.registry.store.editing() as edit:\n            name = caller.own_thread()\n"),
            ("edit.document.require(caller.name).auto_title_pending", "edit.document.require(name).auto_title_pending"),
            ("edit, caller.name, RegistryNames.thread_slug(display_name)", "edit, name, RegistryNames.thread_slug(display_name)"),
        ], "ThreadManagement.rename_self"),
        patch("src/agent_comms/registry/thread_management.py", [
            ("caller: ThreadCaller", "caller: ReadAuthority"),
            ("        with self.registry.store.editing() as edit:\n", "        with self.registry.store.editing() as edit:\n            name = caller.own_thread()\n"),
            ("edit.document.status(caller.name).require_self_rename()", "edit.document.status(name).require_self_rename()"),
            ("thread = edit.document.require(caller.name)", "thread = edit.document.require(name)"),
        ], "ThreadManagement.set_own_title"),
        {"operation": "remove_import_names", "file_path": str(args.root / "src/agent_comms/importing.py"),
         "module_name": "agent_comms.imported_conversation", "import_names": ["KeptPiSession"]},
        patch("tests/test_goal_runtime_annotations.py", [
            ("from collections.abc import Callable", "from collections.abc import AsyncIterator, Callable"),
            ("from agent_comms import agent_events", "from agent_comms import agent_events\nfrom agent_comms.agent_runtime import DeliversInput, PublishesTranscript, ReadsHistory\nfrom agent_comms.cli_bridges.history import History\nfrom agent_comms.execution.native_entries import ImportedSessionWriter\nfrom agent_comms.harness_delivery_states import Outcome\nfrom agent_comms.harness_install import HarnessSession\nfrom agent_comms.importing import ImportBuffer, ImportSnapshot\nfrom agent_comms.runtime_updates import RuntimeUpdate, SessionFacts\nfrom agent_comms.transcript_events import TranscriptEvent"),
            ("def test_declared_policy_keys_keep_their_original_message_and_identity_sources() -> None:",
             runtime_test + "def test_declared_policy_keys_keep_their_original_message_and_identity_sources() -> None:"),
        ]),
    ]
    args.plan.write_text(json.dumps({"recipes": [{
        "recipe_id": "p23-moved-import-consumers-original-authority",
        "reason": (
            "The retained native graph proves no reverse import path from either OpenCode "
            "value into History or from History/RegistryEdit into ThreadManagement. Hoist those "
            "exact original values. The sole remaining registry cycle is ThreadCaller -> board "
            "delivery -> Comms -> ThreadManagement. Its two callbacks only read the bound caller's "
            "own name. Use the existing ReadAuthority.own_thread answer, which actual ThreadCaller "
            "and every current bound caller inherit, rather than create another caller role or "
            "accept a NamedViewer. ReadAuthority's existing unscoped default refuses; no new "
            "admission branch is introduced. Capture that answer inside the original editing "
            "scope, before the original title/status checks, and preserve its own-name identity "
            "instead of replacing it with ActsForTeam's delegated authority. Keep process and "
            "incarnation-fenced managed renames unchanged. All admitted bound callers currently "
            "return their original frozen name; no backend, scheduler or human admission changes. "
            "Delete only the now-unused KeptPiSession re-export left by relocation; ImportSnapshot "
            "and ThreadImported still legitimately import their original value base. Add one "
            "positive original-factory/native-writer/history/delivery callback test; it derives "
            "the source from the public callback instead of importing a proposed new name, so "
            "a source-stashed baseline can fail on the original unresolved callback itself. "
            "This is an authored contract decision; real import, CLI title/rename, locking, "
            "history, byte-preservation and native consumers must still pass."
        ),
        "operations": operations, "architecture_guards": [], "authority_claims": [],
    }], "architecture_guards": []}, indent=2) + "\n")
    print(json.dumps({"operations": len(operations)}))


if __name__ == "__main__":
    main()
