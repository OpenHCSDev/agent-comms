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
from pathlib import Path

from agent_comms.child_process import ProcessIdentity
from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV
from agent_comms.runtime import RuntimeConnection, socket_path
from agent_comms.threads import Thread


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
        agent_args=['--model', 'openai-codex/gpt-6.1-sol', '--thinking', 'off', '--offline'],
        auto_wake=False, runtime_enabled=True,
    )
    name = 'context547'
    process = ProcessIdentity.capture(os.getpid())
    thread = Thread(name, frozenset(), '/home/ts/.agent-comms',
                    process_identity=process, session_file=str(source),
                    model='openai-codex/gpt-6.1-sol', thinking_level='off')
    receipt = {'scope': 'private saved-source owner for actual installed309 UI',
               'root': str(root), 'source': str(source), 'root_id': root_id,
               'package': str(args.package), 'name': name,
               'process': FieldCodec.encode(process), 'python': sys.executable,
               'core_module': inspect.getfile(Comms), 'before_source_hashes': original,
               'configured_model': thread.model, 'thinking': 'off',
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
        (evidence / 'owner-prepared.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt), flush=True)
        # Local harness teardown; this line is never forwarded to native stdin.
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
    asyncio.run(main(parser.parse_args()))
