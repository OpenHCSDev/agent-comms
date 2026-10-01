"""Run existing affected native journeys with installed production imports only."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    source, stage, native, output = map(Path, sys.argv[1:])
    source, stage, native, output = (p.resolve() for p in (source, stage, native, output))
    assert Path(sys.prefix).resolve() == stage
    import agent_comms
    assert Path(agent_comms.__file__).resolve().is_relative_to(stage)
    # Existing test dependencies are borrowed read-only; they contain no product
    # module. Existing tests are the only source import path passed to children.
    deps = Path('/home/ts/wt/g452e/deps')
    assert not (deps / 'agent_comms').exists() and not (deps / 'toad').exists()
    output.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith('AGENT_COMMS_') or key.startswith('PI_CODING_AGENT_'):
            environment.pop(key)
    environment['PYTHONPATH'] = str(deps) + os.pathsep + str(source / 'tests')
    environment['PI_COMPACTION_TEST_PACKAGE'] = str(native)
    environment['AGENT_COMMS_TEST_COPIED_PIN'] = str(native)
    # This gate tests changed tracked completion, not the already accepted
    # removal of the old90s whole-turn deadline. Real bash executes once.
    environment['TRACKED_NATIVE_LONG_TOOL_SECONDS'] = '0.25'
    selected = [
        'tests/test_backend_native_output.py',
        'tests/test_tracked_native_lifecycle.py::test_installed_selected_long_tool_and_uncertain_cleanup[complete]',
        'tests/test_fresh_private_session.py::test_optional_reviewed_copied_pi_reads_selected_bootstrap_level',
        'tests/test_fresh_private_session.py::test_optional_reviewed_copied_pi_preserves_explicit_fresh_inode',
        'tests/test_tool_diffs.py',
    ]
    command = [sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-s',
               '--basetemp=' + str(output / 'roots'), *selected]
    started = time.monotonic()
    with (output / 'terminal.log').open('w') as stream:
        completed = subprocess.run(command, cwd=source, env=environment,
                                   stdout=stream, stderr=subprocess.STDOUT, timeout=120)
    receipt = {
        'state': 'completed', 'exit_code': completed.returncode,
        'duration_seconds': time.monotonic() - started, 'command': command,
        'production_origin': agent_comms.__file__, 'stage': str(stage),
        'native': str(native), 'source_tests': str(source / 'tests'),
        'provider': 'existing controlled localhost fixtures only',
        'paid_calls': 0, 'public_inputs': 0, 'unknown_replays': 0,
        'scope': 'Native terminal failure extensibility, tool recovery/final answer, image failure privacy, tracked actual bash/final completion, native fresh bootstrap/inode and live/replay edit evidence.',
        'not_claimed': 'Whole product readiness, whole-turn deadline retest, performance, public publication.',
    }
    (output / 'terminal-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)
    return completed.returncode


if __name__ == '__main__':
    raise SystemExit(main())
