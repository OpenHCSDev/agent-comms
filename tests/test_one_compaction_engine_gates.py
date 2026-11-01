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

from agent_comms.native_build_inputs import NativeBuildInputs

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src" / "agent_comms"
STACK = REPO / "stack"
NATIVE_INPUT_PATCH = STACK / "native/native-input.patch"

pytestmark = pytest.mark.refactor_guard

# Modules that existed only to run, journal, publish or reconcile Core's engine.
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
    files = [*NativeBuildInputs(REPO).paths(), *(STACK / "bin").iterdir()]
    files += list((SOURCE / "_pi_helpers").glob("*.mjs"))
    return {str(path.relative_to(REPO)): path.read_text() for path in files}


def _constants(tree: ast.AST) -> set[str]:
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


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


def test_no_claim_rule_waits_on_compaction_state():
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
