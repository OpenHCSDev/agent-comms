"""Resolve actual projected callbacks in fresh processes; no provider or live bus runs."""
from __future__ import annotations
import importlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import typing

ROOT = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).parent / 'projected/src'
MODULES = (
    'agent_comms.backend', 'agent_comms.prompt_state',
    'agent_comms.execution.turn_inputs', 'agent_comms.execution.turn_output',
    'agent_comms.execution.owned_turn', 'agent_comms.agent_activity',
    'agent_comms.registry.thread_status', 'agent_comms.registry.registry_document',
)


def inspect_module(name: str) -> None:
    module = importlib.import_module(name)
    failures = []
    checked = []
    for label, value in tuple(vars(module).items()):
        if inspect.isfunction(value) and value.__module__ == name:
            try:
                typing.get_type_hints(value)
                checked.append(label)
            except Exception as error:
                failures.append((label, type(error).__name__, str(error)))
        elif inspect.isclass(value) and value.__module__ == name:
            try:
                typing.get_type_hints(value)
                checked.append(label)
            except Exception as error:
                failures.append((label, type(error).__name__, str(error)))
            for key, descriptor in vars(value).items():
                target = descriptor
                if isinstance(descriptor, (staticmethod, classmethod)):
                    target = descriptor.__func__
                elif isinstance(descriptor, property):
                    target = descriptor.fget
                if inspect.isfunction(target):
                    try:
                        typing.get_type_hints(target)
                        checked.append(label + '.' + key)
                    except Exception as error:
                        failures.append((label + '.' + key, type(error).__name__, str(error)))
    if name == 'agent_comms.backend':
        from agent_comms.backend import NativeSession, TurnSession
        assert inspect.isabstract(NativeSession), 'Original prompt_type contract must remain abstract'
        assert not inspect.isabstract(TurnSession), TurnSession.__abstractmethods__
    print(json.dumps({'module': name, 'file': module.__file__, 'checked': checked, 'failures': failures}), flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] != 'all':
        inspect_module(sys.argv[1])
    else:
        import os
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        results = []
        for name in MODULES:
            result = subprocess.run([sys.executable, str(Path(__file__).absolute()), name],
                                    env=env, cwd=ROOT, capture_output=True, text=True, timeout=45)
            results.append({'module': name, 'returncode': result.returncode,
                            'stdout': result.stdout, 'stderr': result.stderr})
            print(name, result.returncode, flush=True)
        (ROOT.parents[1] / 'cold-v10-results.json').write_text(json.dumps(results, indent=2) + '\n')
        if any(result['returncode'] for result in results):
            raise SystemExit(1)
