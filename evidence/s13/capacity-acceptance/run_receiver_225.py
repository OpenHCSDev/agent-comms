"""Run the existing S13 capacity test against the explicitly assigned 225 receiver.

Only the disposable Thread fixture uses that receiver's real pid constructor.
The watchdog/CLI child owner is the authoritative PR232 A12 source, loaded as a
private test module. No production file, import contract or native package changes.
"""

import argparse
import importlib.util
import os
import sys
import types
from pathlib import Path

from pytest import MonkeyPatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--history-mib', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    receiver = Path('/home/ts/wt/comms-native-session-entry-store-20260928/src')
    import agent_comms

    assert Path(agent_comms.__file__).resolve().parent == receiver / 'agent_comms'
    spec = importlib.util.spec_from_file_location(
        'agent_comms.s13_capacity_child', root / 'src/agent_comms/child_process.py'
    )
    child = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = child
    spec.loader.exec_module(child)
    source_file = root / 'tests/test_owner_compaction_prepare.py'
    source = source_file.read_text()
    source = source.replace(
        'from agent_comms.child_process import AttachedChild, Platform, ProcessIdentity',
        'from agent_comms.s13_capacity_child import AttachedChild, Platform, ProcessIdentity',
    )
    # Explicit fixture migration to this specific receiver, never an API fallback.
    source = source.replace(
        'process_identity=ProcessIdentity.capture(os.getpid()),', 'pid=os.getpid(),'
    )
    module = types.ModuleType('s13_capacity_receiver_225')
    module.__file__ = str(source_file)
    sys.modules[module.__name__] = module
    exec(compile(source, str(source_file), 'exec'), module.__dict__)
    args.output.mkdir(parents=True, exist_ok=False)
    print('RECEIVER_SOURCE', receiver, flush=True)
    print('NATIVE_PACKAGE', os.environ['PI_COMPACTION_TEST_PACKAGE'], flush=True)
    with MonkeyPatch.context() as patch:
        module.test_large_history_cli_prepare_commit_reopen_under_memory_budget(
            args.output.resolve(), patch, args.history_mib
        )


if __name__ == '__main__':
    main()
