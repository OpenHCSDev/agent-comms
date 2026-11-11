"""Carry original P34 declarations through the retained P23 import merge."""

import argparse
import ast
import json
from pathlib import Path

from nominal_refactor_advisor.ast_tools import ModuleSyntaxIndex
from p23_rebase_imports import ModuleMoves


def declared(path: Path) -> set[str]:
    module = ast.parse(path.read_text(), filename=str(path))
    return {node.name for node in module.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("roots", type=Path, nargs="+")
    args = parser.parse_args()
    repository = Path.cwd()
    providers = {
        "agent_comms.acp_extension": ("transport/attachment_scope",),
        "agent_comms.execution.backend": ("execution/finish_signal",),
        "agent_comms.pi_native_backend": ("execution/persistent_pi_session",),
        "agent_comms.execution.native_transcript": (
            "execution/native_record_file", "execution/native_records", "execution/transcript_sources"),
        "agent_comms.transcript.external_transcript": (
            "transcript/external_transcript_rows", "execution/transcript_sources"),
        "agent_comms.transcript.transcripts": (
            "execution/transcript_sources", "transcript/transcript_drafts"),
        "agent_comms.execution.transcript_sources": (
            "execution/native_records", "transcript/transcript_drafts"),
        "agent_comms.importing": (
            "execution/imported_conversation", "execution/imported_session_metadata"),
        "agent_comms.registry.registry_document": ("registry/roster_visibility",),
    }
    projection = ModuleMoves({}, {}, "original-P34-declaration-carry")
    for original, destinations in providers.items():
        for destination in destinations:
            path = repository / "src/agent_comms" / (destination + ".py")
            for name in declared(path):
                key = original, name
                module = "agent_comms." + destination.replace("/", ".")
                if key in projection.symbols and projection.symbols[key] != module:
                    raise ValueError(f"Two declaration owners for {key}: {projection.symbols[key]}, {module}")
                projection.symbols[key] = module
    operations = []
    for root in args.roots:
        for path in sorted(root.resolve().rglob("*.py")):
            before = path.read_text()
            filename = path.relative_to(repository) if path.is_relative_to(repository) else path.relative_to(root.resolve().parent)
            after = projection.normalize(before, str(filename), changed_only=True)
            if before == after:
                continue
            if before.count("# noqa") != after.count("# noqa"):
                raise ValueError(f"Import carry changed noqa markers: {path}")
            ast.parse(after, filename=str(path))
            operations.append({"operation": "patch_target", "file_path": str(path),
                               "replacements": [{"old_source": before, "new_source": after}]})
    missing = []
    for filename, module, name in (
        ("src/agent_comms/history_views.py", "agent_comms.execution.transcript_sources", "TranscriptCursor"),
        ("src/agent_comms/messaging/sending.py", "agent_comms.execution.transcript_sources", "ExternalTranscript"),
        ("src/agent_comms/registry/view_unread.py", "agent_comms.registry.registry_document", "RegistrySnapshot"),
        ("src/agent_comms/pi_native_backend.py", "agent_comms.execution.native_session_reopen", "NativeSessionIdentity"),
    ):
        path = repository / filename
        syntax = ModuleSyntaxIndex.build(ast.parse(path.read_text(), filename=str(path)))
        if any(node.id == name for node in syntax.instances_of(ast.Name)):
            missing.append({"operation": "ensure_import", "file_path": str(path),
                            "import_source": f"from {module} import {name}"})
    reason = "Retained P23/P34 three-way imports follow the original declarations at their current modules. Change import syntax only, retain guards in their original scopes, preserve bindings and noqa markers. No guard promotion, fields, constructors, acquisition or method bodies change. Combined runtime qualification remains separate."
    args.output.write_text(json.dumps({"stages": [
        {"recipes": [{"recipe_id": "p23-p34-original-import-owners", "reason": reason,
                      "operations": operations}]},
        {"recipes": [{"recipe_id": "p23-p34-merged-import-union", "reason": reason,
                      "operations": missing}]},
    ]}, indent=2) + "\n")
    print(json.dumps({"rewritten_files": len(operations), "required_imports": len(missing)}))


if __name__ == "__main__":
    main()
