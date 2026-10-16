"""Actual CLI binding, delivery, persisted reopen and conflict; owned private root only."""
import asyncio
import json
from pathlib import Path
import sys

from agent_comms.cli_bridges.shared import Binding, CoreConnection, DeliveryJournal, DeliveryRow, Outcome


async def main():
    runtime, root, state = map(Path, sys.argv[1:4])
    binding = Binding(str(root), 'codex-drafts', 'source')
    pending = DeliveryRow('retained-pending', 'opaque original payload', Outcome.UNCONFIRMED)
    journal = DeliveryJournal(state)
    with journal.db:
        pending.insert(journal.db)
    journal.close()
    results = []

    async def inbox(arguments, body):
        process = await asyncio.create_subprocess_exec(
            str(runtime / 'bin/agent-comms-claude-bridge'), 'inbox', '--state', str(state), *arguments,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        stderr = []
        try:
            async with asyncio.timeout(15):
                while True:
                    line = await process.stderr.readline()
                    assert line, (process.returncode, stderr)
                    stderr.append(line.decode())
                    if line.startswith(b'Watching '):
                        break
                sender = await asyncio.create_subprocess_exec(
                    str(runtime / 'bin/agent-comms'), '--root', str(root), 'send',
                    '--from', 'opencode-drafts', '--to', binding.bus, '--type', 'info', '--body', body,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    sent_out, sent_err = await sender.communicate()
                finally:
                    await CoreConnection.join(sender)
                print('send:', sent_out.decode(), sent_err.decode(), flush=True)
                assert sender.returncode == 0, (sent_out, sent_err)
                line = await process.stdout.readline()
                received = json.loads(line)
                assert body in received, received
                while True:
                    line = await process.stderr.readline()
                    assert line, stderr
                    stderr.append(line.decode())
                    if line.startswith(b'emitted: '):
                        break
                results.append({'body': body, 'received': received, 'stderr': stderr})
        finally:
            await CoreConnection.join(process)
            out, err = await asyncio.wait_for(process.communicate(), 15)
            assert process.returncode == 0, (out, err, stderr)

    await inbox(['--root', binding.root, '--bus-thread', binding.bus, '--session', binding.thread],
                'journal first delivery')
    await inbox([], 'journal delivery after saved-binding reopen')
    conflict = await asyncio.create_subprocess_exec(
        str(runtime / 'bin/agent-comms-claude-bridge'), 'inbox', '--state', str(state),
        '--root', binding.root, '--bus-thread', 'opencode-drafts', '--session', binding.thread,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(conflict.communicate(), 15)
    finally:
        await CoreConnection.join(conflict)
    assert conflict.returncode != 0 and b'another Core/CLI conversation' in err, (out, err)
    journal = DeliveryJournal(state)
    try:
        assert journal.bind(None) == binding
        assert Binding.select(journal.db) == [binding]
        rows = DeliveryRow.select(journal.db)
        assert len(rows) == 3, rows
        assert DeliveryRow.one(journal.db, id=pending.id) == pending
        assert sum(row.outcome is Outcome.EMITTED for row in rows) == 2
    finally:
        journal.close()
    print(json.dumps({'results': results, 'binding': vars(binding), 'conflict_rejected': True,
                      'pending_unchanged': True, 'emitted_deliveries': 2,
                      'runtime_launcher': str(runtime), 'source_overlay': str(Path.cwd() / 'src'),
                      'provider_inputs': 0}, indent=2), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
