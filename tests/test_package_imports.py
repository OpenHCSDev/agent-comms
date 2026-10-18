"""Entrypoints import their APIs, not the package root's former catalog."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("entrypoint", ["agent_comms.ui_model.observation", "agent_comms.cli"])
def test_entrypoint_keeps_catalog_out_and_thread_lookup_seals_backends(entrypoint):
    source = Path(__file__).parents[1] / "src"
    program = f"""
import importlib, json, sys
sys.path.insert(0, {str(source)!r})
importlib.import_module({entrypoint!r})
from agent_comms.threads import Thread
from agent_comms.agent_backend import AgentBackend
print(json.dumps({{
    'catalog_loaded': 'agent_comms.tools' in sys.modules,
    'sealed': AgentBackend._sealed,
    'native': AgentBackend.decode('pi_native').declared_name,
    'durable': AgentBackend.decode('pi_durable').declared_name,
    'default': AgentBackend.for_new_threads().declared_name,
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program], capture_output=True, text=True, check=True,
    )
    assert json.loads(completed.stdout) == {
        "catalog_loaded": False,
        "sealed": True,
        "native": "pi_native",
        "durable": "pi_durable",
        "default": "pi_native",
    }


def test_saved_tool_record_acquires_its_family_without_package_bootstrap():
    source = Path(__file__).parents[1] / "src"
    program = f"""
import sys
sys.path.insert(0, {str(source)!r})
from dataclasses import dataclass
from agent_comms.field_codec import FieldCodec
from agent_comms.tool_declaration import ToolDeclaration
assert 'agent_comms.tools' not in sys.modules
@dataclass(frozen=True)
class SavedTool:
    tool: ToolDeclaration | None
record = FieldCodec.decode(SavedTool, {{'tool': {{
    'tool': 'comms_edit_goal', 'goal_id': 'saved-goal', 'text': 'retained text'
}}}})
assert record.tool.declared_name == 'comms_edit_goal'
assert record.tool.goal_id == 'saved-goal'
assert record.tool.text == 'retained text'
"""
    subprocess.run([sys.executable, "-I", "-c", program], check=True, capture_output=True, text=True)
