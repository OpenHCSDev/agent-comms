"""Read-only live snapshot; all owner rebinding/observation writes stay disposable."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from unittest.mock import patch

from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.store_files import file_revision

SOURCE = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
FILES = ('registry.json', '.registry-owner-guard', 'bus.jsonl', 'bus_meta.json',
         'catalog.json', 'read_ledger.json', 'activity.jsonl', 'activity_latest.json')
DATABASES = ('coordination.sqlite3', 'compaction-commits.sqlite3', 'native_prompt_bindings.sqlite3')
OWNERS = ('agent-comms-ux', 'pr95-selected-pi-summary-owner')


def forbidden(*args, **kwargs):
    raise AssertionError('Quiescent copied live root must not launch or verify native package')


async def run():
    with tempfile.TemporaryDirectory(prefix='ac-idle-live-copy-', dir='/var/tmp') as directory:
        root = Path(directory)
        before = {name: file_revision(SOURCE / name) for name in FILES + DATABASES}
        for name in FILES:
            shutil.copy2(SOURCE / name, root / name)
        for name in DATABASES:
            with sqlite3.connect(f'file:{SOURCE / name}?mode=ro', uri=True) as source:
                with sqlite3.connect(root / name) as target:
                    source.backup(target)
        assert before == {name: file_revision(SOURCE / name) for name in FILES + DATABASES}, 'Source changed during copy'
        # The original inode certificate cannot attest copied files. Certify the
        # unchanged copied canonical bytes through the existing installer only.
        marker_path = root / 'bus_meta.json'
        metadata = json.loads(marker_path.read_text())
        metadata.pop('checkpoint_version')
        metadata.pop('checkpoint_seal')
        marker_path.write_text(json.dumps(metadata))
        comms = Comms(root)
        install_private_bus_checkpoint(comms.bus.log)
        canonical = (root / 'bus.jsonl').read_bytes()
        def sql_rows():
            with sqlite3.connect(root / 'coordination.sqlite3') as db:
                return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                        for table in ('wake_claims', 'claim_batch_receipts', 'native_runtime_inputs', 'current_executions')}
        original = sql_rows()
        results = {}
        for name in OWNERS:
            owner = comms.registry.require(name)
            assert owner.active_turn is None
            comms.registry.register(replace(owner, pid=os.getpid()), new_owner=True)
            agent = CommsAgent(comms, runtime_enabled=True,
                               private_nk_native_package=root / 'never-launch',
                               private_nk_wire_root_id=metadata['wire_root_id'])
            agent.sessions.bindings[name] = name
            agent.sessions.titles[name] = name
            agent.sessions.worktrees[name] = owner.worktree
            scans = 0
            drain = agent._drain_private_nk
            async def observed(*args):
                nonlocal scans
                scans += 1
                return await drain(*args)
            with patch.object(agent, '_drain_private_nk', observed), \
                 patch('agent_comms.coordinated_runtime.run_native_pi_turn', forbidden), \
                 patch('agent_comms.coordinated_runtime._trusted_package', forbidden), \
                 patch('agent_comms.cohort_foreground._trusted_package', forbidden):
                cold = [await agent.inputs.drain_inbox(name) for _ in range(3)]
                settled = scans
                revision = file_revision(root / 'coordination.sqlite3')
                warm = [await agent.inputs.drain_inbox(name) for _ in range(20)]
                assert not any(cold + warm)
                assert scans == settled and name in agent.inputs._idle_private_revisions
                assert file_revision(root / 'coordination.sqlite3') == revision
                results[name] = {'cold_scans': settled, 'warm_polls': 20, 'warm_scans': scans-settled,
                                 'native_launches': 0, 'coordinator_revision_stable': True}
        assert (root / 'bus.jsonl').read_bytes() == canonical
        assert sql_rows() == original
        print(json.dumps({'source': str(SOURCE), 'sequence': metadata['last_seq'], 'owners': results,
                          'claims_inputs_execution_pointers_unchanged': True,
                          'canonical_bus_unchanged': True, 'copy_removed_on_exit': True}, indent=2))


asyncio.run(run())
