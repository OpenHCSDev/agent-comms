"""Exercise the same continuous original-input journey through installed CLI."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import agent_comms


def main():
    checkout = Path(__file__).resolve().parents[2]
    installed = Path(agent_comms.__file__).resolve().parent
    if checkout / "src" in installed.parents or "site-packages" not in installed.parts:
        raise ValueError("Installed original-input pilot cannot use a source overlay")
    stage = Path(sys.argv[1]).absolute()
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    hashes = {}
    for name in ("task_sources.py", "cli_commands.py", "retained_context.py",
                 "context_segments/retained.py", "wire_log.py",
                 "input_attempt.py", "input_origin.py", "retained_task_facts.py", "turn_context.py"):
        actual = (installed / name).read_bytes()
        assert actual == (checkout / "src/agent_comms" / name).read_bytes()
        hashes[name] = hashlib.sha256(actual).hexdigest()
    sys.path.insert(0, str(checkout / "tests"))
    spec = importlib.util.spec_from_file_location(
        "original_input_controls", checkout / "tests/test_retained_context_operations.py")
    controls = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controls)
    calls = []
    cli = str(Path(sys.executable).parent / "agent-comms")

    def command(root, args):
        result = subprocess.run([cli, "--root", str(root), *args],
                                capture_output=True, text=True, timeout=30, check=False)
        payload = json.loads(result.stdout)
        calls.append({"command": args[0], "exit_code": result.returncode})
        return result.returncode, payload

    journey = controls.original_input_consumer_journey(stage / "saved-state", command)
    receipt = {"state": "INSTALLED_ORIGINAL_INPUT_CONSUMER_JOURNEY_PASS",
               "installed_package": str(installed), "installed_cli": cli,
               "module_sha256": hashes, "calls": calls, "journey": journey,
               "source_overlay": False, "conftest_scheduler_mock": False,
               "native_builds": 0, "full_native_ACP_UI_acceptance": False}
    (stage / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
