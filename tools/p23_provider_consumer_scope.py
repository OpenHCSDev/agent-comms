"""Read the regression's required pairs once and select their existing consumers."""

import json
from pathlib import Path
import subprocess

import test_nominal_provider_contracts as regression

from agent_comms.execution.turn_failure import TurnFailure
from agent_comms.planning.goals import ProblemGoalOrder
from agent_comms.planning.orders import Todo

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    mark, = regression.test_original_providers_declare_the_required_read_capabilities.pytestmark
    pairs = mark.args[1]
    names = {declaration.__name__ for pair in pairs for declaration in pair}
    names.update((ProblemGoalOrder.__name__, Todo.goal_instructions.__name__,
                  TurnFailure.prepare_execution_retry.__name__))
    command = ["git", "grep", "-l", "-w"]
    for name in sorted(names):
        command.extend(("-e", name))
    command.extend(("--", "tests"))
    found = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    if found.returncode not in (0, 1):
        raise RuntimeError(found.stderr)
    matched = {line for line in found.stdout.splitlines() if line.endswith(".py")}
    files = {name for name in matched if Path(name).name.startswith("test_")}
    files.update(("tests/test_nominal_provider_contracts.py", "tests/test_repository_name.py",
                  "tests/test_storage_rules.py"))
    print(json.dumps({"declarations": sorted(names), "test_files": sorted(files),
                      "support_files": sorted(matched - files)}, indent=2))


if __name__ == "__main__":
    main()
