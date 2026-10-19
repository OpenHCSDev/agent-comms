"""Read a copied saved session through real ACP; never submit or replay input.

env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=tests <installed-python> \
    tests/transcript_event_time_installed_journey.py \
    --root <new-private-directory> --session <saved-session.jsonl> --native-package <package>
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import acp
import agent_comms
from agent_comms import native_package
from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import wire
from delivery_owner_fixture import canonical_agent
from native_backend_fixture import native_backend_fixture


async def run(args):
    original = args.session.read_bytes()
    expected = [datetime.fromisoformat(row['timestamp']).timestamp()
                for row in map(json.loads, original.splitlines()) if row['type'] == 'message']
    assert expected
    package = native_package.VerifiedNativePackage.acquire(args.native_package)
    args.root.mkdir(mode=0o700)
    with patch.object(native_package, 'MANIFEST', package.path.parents[3] / 'pi-native.sha256'):
        async with native_backend_fixture(args.root, package=package) as native:
            copies = {args.session: native.session}
            originals = {args.session: original}
            proof = args.session.with_suffix(args.session.suffix + '.input-proof')
            if proof.exists():
                copies[proof] = native.session.with_suffix(native.session.suffix + '.input-proof')
                originals[proof] = proof.read_bytes()
            for source, destination in copies.items():
                shutil.copy2(source, destination)
            comms = wire(native.root)
            comms.registry.declare(comms.registry.new_thread(
                'clock', frozenset(), str(native.project), session_file=str(native.session)))
            first = comms.transcripts.thread_transcript_page('clock')
            assert [event.timestamp for event in first.events] == expected
            snapshots = asyncio.Queue()

            class Client(acp.Client):
                async def session_update(self, session_id, update, **kwargs):
                    for fact in decode_updates(update.field_meta):
                        if isinstance(fact, TranscriptSnapshotUpdate):
                            snapshots.put_nowait(fact.page)

            owner = canonical_agent(comms, auto_wake=False, agent_bin='pi',
                                    agent_args=native.native_arguments())
            connections = set()

            async def serve(reader, writer):
                task = asyncio.current_task()
                connections.add(task)
                try:
                    await acp.run_agent(owner, writer, reader)
                finally:
                    writer.close()
                    await writer.wait_closed()
                    connections.remove(task)

            server = await asyncio.start_server(serve, '127.0.0.1', 0)
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            client = acp.connect_to_agent(Client(), writer, reader)
            try:
                async with asyncio.timeout(20):
                    await client.initialize(protocol_version=1)
                    for _ in range(2):
                        await client.load_session(cwd=str(native.project), session_id='clock', mcp_servers=[])
                        received = await snapshots.get()
                        assert received.events == first.events
                        assert received.before == first.before and received.after == first.after
                        assert received.snapshot.same_snapshot(first.snapshot)
                        assert [event.timestamp for event in received.events] == expected
                rebuilt = wire(native.root).transcripts.thread_transcript_page('clock', through=first.after)
                assert rebuilt.events == first.events
                assert rebuilt.before == first.before and rebuilt.after == first.after
                assert rebuilt.snapshot.records_cut == first.snapshot.records_cut
                assert rebuilt.snapshot.through == first.after
                for source, destination in copies.items():
                    assert source.read_bytes() == originals[source]
                    assert destination.read_bytes() == originals[source]
                assert native.provider.posts == 0
                print(json.dumps({'core': agent_comms.__file__, 'root': str(args.root),
                    'saved_source': str(args.session), 'sha256': hashlib.sha256(original).hexdigest(),
                    'original_clocks': expected, 'acp_loads': 2, 'rebuild_exact_events': True,
                    'source_and_copy_unchanged': True, 'provider_posts': native.provider.posts}))
            finally:
                writer.close()
                await writer.wait_closed()
                await client.close()
                server.close()
                for task in tuple(connections):
                    task.cancel()
                await asyncio.gather(*tuple(connections), return_exceptions=True)
                await server.wait_closed()
                await owner.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--session', required=True, type=Path)
    parser.add_argument('--native-package', required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run(args))
