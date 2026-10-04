"""Installed original idle-observation reader; never drain or launch an owner."""
import asyncio
import hashlib
from importlib import metadata
import json
from pathlib import Path
import subprocess
import sys
import time

import agent_comms
from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec

here = Path(__file__).resolve().parent
checkout = here.parents[1]
root = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
native = Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/stack/.pi-native-960296fdddafb01c/node_modules/@earendil-works/pi-coding-agent')

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def revision(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

async def main():
    package = Path(agent_comms.__file__).parent
    source = checkout / 'src/agent_comms'
    assert not package.is_relative_to(source)
    hashes = {str(path.relative_to(source)): digest(path) for path in source.rglob('*')
              if path.is_file() and '__pycache__' not in path.parts}
    for name, expected in hashes.items():
        assert digest(package / name) == expected, name
    (here / 'installed-source-proof.json').write_text(json.dumps(hashes, indent=2) + '\n')

    # Constructors compose the original read resources. No session load,
    # declaration, configuration refresh, wake, cursor advance or drain is called.
    comms = Comms(root, private_initial_writes=False, private_claim_writes=False)
    original = comms.registry.snapshot()
    owners = [thread for name, thread in original.threads.items()
              if original.statuses[name].active and thread.process_alive]
    assert len(owners) == 19, len(owners)
    for thread in owners:
        thread.require_idle()
    proofs = [Path(thread.session_file + '.input-proof') for thread in owners
              if thread.session_file and Path(thread.session_file + '.input-proof').is_file()]
    protected = {str(path): digest(path) for path in proofs}
    sources = {thread.name: revision(Path(thread.session_file)) for thread in owners
               if thread.session_file}
    marker_before = digest(root / 'bus_meta.json')
    wire_before = revision(root / 'bus.jsonl')
    default_before = Path('/home/ts/.local/bin/agent-comms').resolve()
    root_id = await comms.bus.log.read_certified_async(lambda source: source.marker.root_id)
    agent = CommsAgent(comms, runtime_enabled=False, private_nk_native_package=native,
                       private_nk_wire_root_id=root_id)
    for thread in owners:
        # Original controller bindings only; this observer has no runtime/client
        # or native child. Original registry/SQL, not these bindings, own custody.
        agent.sessions.bindings[thread.name] = thread.name
        agent.sessions.titles[thread.name] = thread.name
        agent.sessions.worktrees[thread.name] = thread.worktree

    async def observe(thread):
        began = time.monotonic()
        result = await agent.inputs._observe_private_revision(thread.name, root_id)
        owner, status, participant, pending, cursor = result[9:14]
        assert owner.thread == thread
        assert owner.admission_generation == original.admission_generations[thread.name]
        owner.thread.require_idle()
        assert status.active
        return {'thread': thread.name, 'incarnation': FieldCodec.encode(thread.incarnation),
                'process': FieldCodec.encode(thread.process_identity),
                'admission': owner.admission_generation,
                'participant_generation': participant.participant_generation,
                'pending_wire_seq': pending.wire_seq if pending else None,
                'cursor_present': cursor is not None,
                'elapsed_seconds': time.monotonic() - began}

    began = time.monotonic()
    observations = await asyncio.wait_for(asyncio.gather(*(observe(t) for t in owners)), 30)
    elapsed = time.monotonic() - began
    current = comms.registry.snapshot()
    for thread in owners:
        assert current.require(thread.name) == thread
        assert thread.process_alive
        assert thread.name not in agent.turns.turn_tasks
    assert not agent.sessions.proxies
    assert {str(path): digest(path) for path in proofs} == protected
    assert {thread.name: revision(Path(thread.session_file)) for thread in owners
            if thread.session_file} == sources
    assert digest(root / 'bus_meta.json') == marker_before
    assert revision(root / 'bus.jsonl') == wire_before
    assert Path('/home/ts/.local/bin/agent-comms').resolve() == default_before
    receipt = {
        'result': 'INSTALLED_READ_ONLY_19_IDLE_OWNER_OBSERVATIONS_PASS',
        'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip(),
        'python': sys.executable, 'package': str(package), 'root': str(root), 'root_id': root_id,
        'direct_url': json.loads(metadata.distribution('agent-comms').read_text('direct_url.json')),
        'sdk': metadata.version('agent-client-protocol'), 'source_files_equal': len(hashes),
        'elapsed_seconds': elapsed, 'observations': observations,
        'source_revisions_unchanged': sources, 'input_proof_hashes_unchanged': protected,
        'wire_revision_unchanged': wire_before, 'marker_sha256_unchanged': marker_before,
        'public_owner_changes': 0, 'inputs_or_drains_or_provider_calls': 0,
        'owned_child_processes': [],
        'limits': 'Detached installed observer, not patched public workers or physical UI. Runtime disabled; original registry/participant/pending/cursor reads only. No throughput or terminal latency claim.',
    }
    (here / 'installed-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'result': receipt['result'], 'owners': len(observations),
                      'source_files_equal': len(hashes), 'elapsed_seconds': elapsed}))

asyncio.run(main())
