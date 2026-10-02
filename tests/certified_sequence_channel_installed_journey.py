"""Installed ordinary saved-owner startup, channel click and certified history.

Uses the existing physical recorder and real app/ACP/root. No input or provider
call is submitted; canonical painted-view ledger updates are expected.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--recorder', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--thread', default='openhcs-helper')
    args = parser.parse_args()
    candidate = args.candidate.resolve(strict=True)
    assert candidate == Path(sys.prefix).resolve()
    spec = importlib.util.spec_from_file_location('certified_sequence_recorder', args.recorder)
    recorder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = recorder
    spec.loader.exec_module(recorder)

    class CertifiedSequenceChannelJourney(recorder.PhysicalJourney):
        @classmethod
        def script(cls, options):
            mark = recorder.marker_command()
            state = lambda label: str(options.output / f'phase-{label}-state.pickle')
            return '\n'.join((
                mark + 'saved-startup',
                'key ctrl+g', 'sleep 1', 'key ctrl+b', 'sleep 2', mark + 'channel-bar',
                recorder.native_click_command(state('channel-bar'), target='channel', name='#openhcs'),
                'sleep 3', mark + 'channel-painted',
                recorder.native_click_command(state('channel-painted'), target='history'),
                'key End', 'sleep 2', mark + 'channel-end', '',
            ))

    output = args.output.resolve()
    assert output.is_relative_to(Path.home() / '.cache/agent-scratch')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = Path.home() / '.local/state/toad/toad.db'
    local = output / 'state/toad/toad.db'
    local.parent.mkdir(parents=True)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
        with sqlite3.connect(local) as copied:
            original.backup(copied)
    os.environ['XDG_STATE_HOME'] = str(output / 'state')
    os.environ['AGENT_COMMS_RUNTIME_ROOT'] = str(candidate / 'bin')
    os.environ['AGENT_COMMS_ACP_LAUNCHER'] = str(candidate / 'bin/agent-comms-acp')
    for key in ('PYTHONPATH', 'AGENT_COMMS_ROOT', 'AGENT_COMMS_THREAD', 'AGENT_COMMS_MANAGED',
                'PI_AGENT_ID', 'PI_PARENT_ID', 'PI_TASK', 'PI_WORKTREE', 'PI_PROMPT'):
        os.environ.pop(key, None)

    from agent_comms.comms import wire
    from agent_comms.field_codec import FieldCodec
    from agent_comms.read_ledger import ReadLedger
    service = wire()
    root = service.root

    def protected():
        thread = service.registry.require(args.thread)
        native = Path(thread.session_file)
        with sqlite3.connect((root / 'coordination.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
            connection.execute('PRAGMA query_only=ON')
            counts = {name: connection.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                      for name in ('native_runtime_input', 'executions', 'wake_claims')}
        return {'thread': FieldCodec.encode(thread), 'native_sha256': hashlib.sha256(native.read_bytes()).hexdigest(),
                'native_bytes': native.stat().st_size, 'bus_sha256': hashlib.sha256(service.bus.log.path.read_bytes()).hexdigest(),
                'counts': counts}

    before = protected()
    (output / 'protected-before.json').write_text(json.dumps(before, indent=2) + '\n')
    ledger = root / ReadLedger.filename
    ledger_before = ledger.read_bytes() if ledger.exists() else b''
    (output / 'read-ledger-before.private').write_bytes(ledger_before)
    arguments = [str(args.recorder), '--output', str(output / 'physical'), '--owner', 'Arendt-certified508',
                 '--capture-target', 'existing_thread', '--journey', CertifiedSequenceChannelJourney.declared_name,
                 '--capture-state', '--review-timing', 'deferred', '--fps', '15', '--width', '1280', '--height', '900',
                 '--fit-window', '--startup-wait', '12', '--max-duration', '55', '--finalize-seconds', '16',
                 '--tail-seconds', '2', '--', '/home/ts/bin/toad-comms', args.thread]
    sys.argv = arguments
    try:
        recorder.main()
    finally:
        after = protected()
        (output / 'protected-after.json').write_text(json.dumps(after, indent=2) + '\n')
        ledger_after = ledger.read_bytes() if ledger.exists() else b''
        (output / 'read-ledger-after.private').write_bytes(ledger_after)
        (output / 'protection.json').write_text(json.dumps({
            'protected_equal': before == after,
            'read_ledger': {'before_sha256': hashlib.sha256(ledger_before).hexdigest(),
                            'after_sha256': hashlib.sha256(ledger_after).hexdigest(),
                            'changed': ledger_before != ledger_after},
            'public_messages_submitted': 0, 'provider_calls_submitted': 0, 'owner_restarts': 0,
        }, indent=2) + '\n')
    assert before == after, 'Original owner/source/input state changed; inspect original proof'


if __name__ == '__main__':
    main()
