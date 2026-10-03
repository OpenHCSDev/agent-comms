"""Hold the existing canonical native owner for the paired309 physical reader.

Run ONLY with Sch's released installed receiver. Reuses the original context
test's owner/protocol setup; never dispatches a prompt. Enter on stdin retires
only this private owner after its physical client has closed.
"""

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import sys
import time
from functools import partial
from pathlib import Path

from agent_comms.child_process import ProcessIdentity
from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV
from agent_comms.runtime import RuntimeConnection, socket_path
from agent_comms.threads import Thread
from agent_comms.coordinator import Coordination


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


async def main(args):
    root, source, evidence = args.root, args.source, args.evidence
    reattachment = root.exists()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    original = {str(path): digest(path) for path in
                (source, source.with_suffix(source.suffix + '.input-proof'))}
    os.environ['PI_COMPACTION_TEST_PACKAGE'] = str(args.package)
    comms = Comms(root)
    if reattachment:
        with comms.bus.log.locked():
            metadata = comms.bus.log.read_metadata_unlocked()
        if not metadata.private:
            raise AssertionError('Reattachment requires the original private root')
        root_id = metadata.root_id
        previous = comms.registry.require('context547')
        previous.require_idle()
        if previous.require_process().alive():
            raise AssertionError('Original private owner is still alive')
        if previous.session_file != str(source):
            raise AssertionError('Reattachment changed the original saved source')
    else:
        root_id = comms.messaging.initialize_private_initial_protocol()
    os.environ.update({ROOT_ID_ENV: root_id, PACKAGE_ENV: str(args.package),
                       'AGENT_COMMS_ROOT': str(root)})
    owner = CommsAgent(
        comms, agent_bin='pi',
        private_nk_native_package=args.package, private_nk_wire_root_id=root_id,
        agent_args=['--model', args.model, '--thinking', args.thinking, '--offline'],
        auto_wake=False, runtime_enabled=True,
    )
    name = 'context547'
    process = ProcessIdentity.capture(os.getpid())
    thread = Thread(name, frozenset(), str(args.worktree),
                    process_identity=process, session_file=str(source),
                    model=args.model, thinking_level=args.thinking)
    receipt = {'scope': 'private saved-source owner and installed native context RPC',
               'root': str(root), 'source': str(source), 'root_id': root_id,
               'package': str(args.package), 'name': name,
               'process': FieldCodec.encode(process), 'python': sys.executable,
               'core_module': inspect.getfile(Comms), 'before_source_hashes': original,
               'configured_model': thread.model, 'thinking': args.thinking,
               'auto_wake': False, 'prompt_dispatched': False,
               'reattached_original_private_root': reattachment}
    try:
        await owner._runtime.start()
        comms.registry.declare(thread)
        await owner.load_session(thread.worktree, name)
        if not reattachment:
            connection = RuntimeConnection(comms, name, socket_path(root, process.pid))
            try:
                try:
                    await connection.request('context')
                except RuntimeError as error:
                    if 'requires an acquired native child' not in str(error):
                        raise
                    receipt['unopened_refused'] = str(error)
                else:
                    raise AssertionError('Unopened context inspection acquired a child')
            finally:
                await connection.close()
        if original != {path: digest(Path(path)) for path in original}:
            raise AssertionError('Unopened observation altered original source')
        await owner.turns.prepare_selected_session(name, thread)
        child = owner.turns.persistent_backends[name].custody.idle().child.proc
        receipt['native_process'] = FieldCodec.encode(child.identity)
        receipt['prepared_child_alive'] = child.alive()
        if original != {path: digest(Path(path)) for path in original}:
            raise AssertionError('Selected startup altered original saved source')
        if args.prepare_compaction:
            from agent_comms.native_entries import NativeEvidenceRead
            from agent_comms.pi_vocabulary import ManualCompactionReason
            from agent_comms.retained_task_facts import RetainedTaskFacts
            from agent_comms.selected_pi_route import (
                observe_selected_compaction_decision, prepare_selected_native_source,
            )
            persistent = owner.turns.persistent_backends[name]
            acquired = persistent.custody.idle()
            selected = acquired.child.attestation.state.model.for_compaction(thread.model)
            settings = await observe_selected_compaction_decision(
                persistent, session_file=str(source), expected_package=args.package,
                selected=selected, purpose=ManualCompactionReason,
            )
            started = time.monotonic()
            initial = await prepare_selected_native_source(
                persistent, session_file=str(source), expected_package=args.package,
                selected=selected, settings=settings.summary_settings(),
            )
            initial_seconds = time.monotonic() - started
            with NativeEvidenceRead.open(source) as reader:
                facts = await Coordination.run_worker(partial(
                    initial.require_ready().witness.retained_task_facts, reader,
                ))
            retained = RetainedTaskFacts(facts)
            started = time.monotonic()
            allocated = await prepare_selected_native_source(
                persistent, session_file=str(source), expected_package=args.package,
                selected=selected, settings=settings.summary_settings(),
                retained_text=retained.text,
            )
            retained_seconds = time.monotonic() - started
            if persistent.custody.idle() is not acquired:
                raise AssertionError('Dry preparation replaced the original native child')
            if original != {path: digest(Path(path)) for path in original}:
                raise AssertionError('Dry preparation changed original source/proof')
            receipt['scope'] = 'installed configured saved-owner native dry preparations; no provider/input/UI'
            receipt['preparation'] = {
                'selected': FieldCodec.encode(selected), 'settings': FieldCodec.encode(settings),
                'initial': FieldCodec.encode(initial), 'allocated': FieldCodec.encode(allocated),
                'initial_seconds': initial_seconds, 'retained_seconds': retained_seconds,
                'retained_fact_count': len(facts),
                'retained_bytes': len(retained.text.encode()),
                'retained_sha256': hashlib.sha256(retained.text.encode()).hexdigest(),
                'same_acquired_child': True,
            }
        else:
            connection = RuntimeConnection(comms, name, socket_path(root, process.pid))
            try:
                observed = await connection.request('context')
                receipt['prepared_context_rpc_sha256'] = hashlib.sha256(
                    json.dumps(observed, sort_keys=True).encode()
                ).hexdigest()
            finally:
                await connection.close()
        (evidence / 'owner-prepared.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt), flush=True)
        # Local harness teardown; this line is never forwarded to native stdin.
        if not args.prepare_compaction:
            control = asyncio.StreamReader()
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(control), sys.stdin
            )
            try:
                # The sole physical recorder bounds its run; its explicit cleanup
                # releases this original local stdin lifetime. EOF also retires it.
                await control.readline()
            finally:
                transport.close()
        await owner.turns.persistent_backends[name].close_idle()
        if child.alive():
            raise AssertionError('Prepared native child survived owned retirement')
        receipt['native_child_exited'] = True
        if original != {path: digest(Path(path)) for path in original}:
            raise AssertionError('Physical reader altered original source')
        receipt['source_hashes_unchanged'] = True
        receipt['result'] = 'PASS'
    finally:
        await owner.shutdown()
        receipt['owner_runtime_closed'] = True
        (evidence / 'owner-terminal.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--model', default='openai-codex/gpt-6.1-sol')
    parser.add_argument('--thinking', default='off')
    parser.add_argument('--worktree', type=Path, default=Path('/home/ts/.agent-comms'))
    parser.add_argument('--prepare-compaction', action='store_true',
                        help='Observe both native dry cuts on the acquired configured saved source')
    asyncio.run(main(parser.parse_args()))
