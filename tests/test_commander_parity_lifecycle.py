"""The common comparison process owner preserves its callers' process contract."""

import json
import sys
from pathlib import Path

from agent_comms.child_process import ExitedOutcome
from agent_comms.commander_parity import lifecycle
from agent_comms.commander_parity.lifecycle import LifecycleRun


def test_command_preserves_explicit_project_and_sanitized_environment(tmp_path: Path):
    result = LifecycleRun.invoke_command(
        (
            sys.executable,
            "-c",
            "import json,os; print(json.dumps([os.getcwd(), "
            "os.environ.get('PARITY_PROCESS'), 'NODE_OPTIONS' in os.environ]))",
        ),
        cwd=tmp_path,
        env={"PARITY_PROCESS": "actual child"},
    )
    assert result.outcome == ExitedOutcome(0)
    assert json.loads(result.stdout) == [str(tmp_path), "actual child", False]


def test_python_adapter_uses_the_candidate_source_environment():
    result = LifecycleRun.invoke("import os; print(os.environ['PYTHONPATH'])")
    assert result.outcome == ExitedOutcome(0)
    assert result.stdout.decode().strip() == str(Path(lifecycle.__file__).resolve().parents[2])
