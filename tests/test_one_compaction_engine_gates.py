"""AST gates: Pi's in-process engine is the only compaction engine Core reaches.

Source evidence only. Every Python module under src/agent_comms must parse or
the gate fails; dynamic attribute access is not resolved. The JavaScript and
patch files of the native stack are not Python, so their gates are text scans
of the exact spellings the deleted engine used.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src" / "agent_comms"
STACK = REPO / "stack"
NATIVE_INPUT_PATCH = REPO / "experiments" / "pi-context-proof" / "pi-0.85.1-native-input.patch"

pytestmark = pytest.mark.refactor_guard

# Modules that existed only to run, journal, publish or reconcile Core's engine.
DELETED_MODULES = re.compile(
    r"^(compaction_\w+|owner_compaction_\w+|selected_pi_route|selected_pi_summary_rpc"
    r"|selected_summary_admission|native_compaction_\w+|continued_private_session"
    r"|failed_input_contexts|context_tokens|pi_summary_payloads|transcript_outcomes"
    r"|turn_input_binding|native_revision_text|selected_source)$"
)
DELETED_NATIVE_FILES = (
    "native-compaction-selected-summary.mjs", "native-compaction-source.mjs",
    "native-compaction-source.d.ts", "native-compaction-policy.mjs",
    "native-compaction-commit-child.mjs", "native-session-context.mjs",
    "native-context-budget.mjs", "inspect-native-compaction-plan.mjs",
    "patch-native-selected-compaction-summary.py", "patch-native-adaptive-settings.py",
    "patch-native-context-budget.py", "patch-native-auto-compaction.py",
    "native-summary-prefix.patch", "native-generation-policy.patch",
)
CUSTOM_COMPACTION_RPCS = (
    "agent_comms_summarize_compaction", "agent_comms_compaction_settings",
    "agent_comms_prepare_compaction", "agent_comms_restore_compaction",
    "agent_comms_cancel_summary",
)


def _modules() -> dict[str, ast.Module]:
    return {
        str(path.relative_to(SOURCE)): ast.parse(path.read_text(), str(path))
        for path in SOURCE.rglob("*.py")
    }


def _stack_texts() -> dict[str, str]:
    files = [
        path for path in STACK.iterdir()
        if path.is_file() and path.suffix in {".mjs", ".py", ".patch", ".ts", ".json"}
    ]
    files += [*(STACK / "bin").iterdir(), NATIVE_INPUT_PATCH]
    files += list((SOURCE / "_pi_helpers").glob("*.mjs"))
    return {str(path.relative_to(REPO)): path.read_text() for path in files}


def _constants(tree: ast.AST) -> set[str]:
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def test_deleted_engine_modules_have_no_definition_or_importer():
    modules = _modules()
    defined = [name for name in modules if DELETED_MODULES.match(Path(name).stem)]
    assert defined == []
    importers = []
    for module, tree in modules.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # "from .x import y" names module x; "from . import x" names module x.
                package_import = node.module in (None, "agent_comms")
                imported = ([alias.name for alias in node.names] if package_import
                            else [node.module.split(".")[-1]])
                if any(DELETED_MODULES.match(name) for name in imported):
                    importers.append(f"{module}:{node.lineno}")
    assert importers == []


def test_deleted_native_engine_files_are_gone():
    assert [name for name in DELETED_NATIVE_FILES if (STACK / name).exists()] == []
    helpers = {path.name for path in (SOURCE / "_pi_helpers").iterdir()}
    assert helpers.isdisjoint({"prepare_compaction.mjs", "context_tokens.mjs"})


def test_no_compaction_journal_store_is_spelled():
    spelled = [module for module, tree in _modules().items()
               if any("compaction-commits" in value for value in _constants(tree))]
    spelled += [name for name, text in _stack_texts().items() if "compaction-commits" in text]
    assert spelled == []


def test_no_custom_compaction_rpc_is_declared_or_sent():
    commands = [
        f"{module}:{node.lineno}:{node.name}"
        for module, tree in _modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and re.fullmatch(r"AgentComms\w*(Compaction|Summary|Summarize)\w*", node.name)
    ]
    spelled = [module for module, tree in _modules().items()
               if _constants(tree) & set(CUSTOM_COMPACTION_RPCS)]
    native = [f"{name}:{rpc}" for name, text in _stack_texts().items()
              for rpc in CUSTOM_COMPACTION_RPCS if rpc in text]
    assert (commands, spelled, native) == ([], [], [])


def test_native_config_directory_is_not_a_managed_compaction_switch():
    """The variable names Pi's canonical auth/models/settings directory, nothing else."""
    users = sorted(name for name, text in _stack_texts().items()
                   if "AGENT_COMMS_NATIVE_CONFIG_DIR" in text
                   and not Path(name).name.startswith("test-"))
    assert users == ["stack/patch-native-model-config.py"]
    switches = re.compile(
        r"(if\s*\(\s*process\.env\.AGENT_COMMS_NATIVE_CONFIG_DIR\s*\))"
        r"|(process\.env\.AGENT_COMMS_NATIVE_CONFIG_DIR\s*\|\|)"
        r"|(AGENT_COMMS_NATIVE_CONFIG_DIR\s*\?\s*(false|0|true))"
    )
    assert [name for name, text in _stack_texts().items() if switches.search(text)] == []
    python_users = sorted(module for module, tree in _modules().items()
                          if "AGENT_COMMS_NATIVE_CONFIG_DIR" in _constants(tree))
    assert python_users == ["owner_launch.py"]


def test_one_context_size_estimator_is_reachable_from_core():
    """Core reports context size only through Pi's get_session_stats (Pi's estimator)."""
    estimators = [
        f"{module}:{node.lineno}:{node.name}"
        for module, tree in _modules().items()
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and re.search(r"estimat|token_?count|count_?tokens|TokenCounter|ContextBudget",
                      node.name, re.IGNORECASE)
    ]
    assert estimators == []
    stats_senders = sorted(
        module for module, tree in _modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", None)) == "GetSessionStats"
    )
    # PiNativeBackend.context_usage, and the same backend's in-turn statistics.
    assert stats_senders == ["pi_native_backend.py", "turn_stats.py"]
    # No helper program and no native-stack file brings a second estimator.
    second = re.compile(r"ContextBudget|estimateSerializedRequestTokens|agent-comms-context-budget")
    assert [name for name, text in _stack_texts().items() if second.search(text)] == []
    helpers = [path.name for path in (SOURCE / "_pi_helpers").glob("*.mjs")
               if re.search(r"estimate\w*Tokens", path.read_text())]
    assert helpers == []


def test_pi_reports_compaction_only_through_its_own_events():
    from agent_comms.pi_native_backend import PI_0_85_1_EVENT_KINDS

    compaction_kinds = {kind for kind in PI_0_85_1_EVENT_KINDS if "compaction" in kind}
    assert compaction_kinds == {"compaction_start", "compaction_end"}


def test_native_input_patch_leaves_pis_compaction_on_for_tracked_inputs():
    """The three tracked-input exclusions that switched Pi's own engine off are gone."""
    text = NATIVE_INPUT_PATCH.read_text()
    added = [line[1:] for line in text.splitlines() if line.startswith("+")]
    assert not any("_nativeRunHadTrackedInput || !model" in line for line in added)
    assert not any("lastAssistant && !options?.inputId" in line for line in added)


def test_no_admission_rule_waits_on_compaction_state():
    """The inbox's send checks no longer consult a compaction barrier or recorded coverage."""
    rules = [
        f"{module}:{node.name}"
        for module, tree in _modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and re.search(r"Journal|Compaction|Coverage", node.name)
        and any(getattr(base, "id", getattr(base, "attr", "")) in {"ReservationRule", "RuleCheck"}
                for base in node.bases)
    ]
    assert rules == []
