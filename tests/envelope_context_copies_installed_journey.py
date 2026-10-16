"""Count one exact doctrine block in saved before/after SDK forks, without input.

The before prompt is the retained actual provider request, checked against its
SDK-forked history. The after prompt is the installed SDK's current preview of
the migrated history plus Core's next-turn contributors. Neither submits input.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import agent_comms

from agent_comms.context_segments.doctrine import DoctrineSegment
from agent_comms.native_entries import NativeEntry
from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
from agent_comms.pi_commands import AgentCommsInspectContext
from agent_comms.turn_context import TurnContext
from envelope_session_installed_journey import message_text
from native_backend_fixture import native_backend_fixture
from stored_inputs import stored_inputs


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def saved_copies(path: Path, text: str) -> int:
    with NativeEntry.open_evidence(path) as reader:
        _, entries = reader.observe()
        return sum(entry.transcript_text().count(text) for entry in entries)


async def run(root: Path, before_session: Path, before_request: Path, after_session: Path):
    assert Path(agent_comms.__file__).is_relative_to(sys.prefix), agent_comms.__file__
    root.mkdir(mode=0o700)  # An interrupted attempt is never reused.
    originals = {str(path): digest(path) for path in (before_session, before_request, after_session)}
    text = DoctrineSegment.capture().text().strip()
    recorded = json.loads(before_request.read_text())
    before_count = sum(message_text(row).count(text) for row in recorded['messages'])
    package = Path(os.environ['PI_COMPACTION_TEST_PACKAGE'])
    report = {'state': 'unfinished', 'provider_inputs': 0, 'paid_calls': 0,
              'core': agent_comms.__file__, 'doctrine_sha256': hashlib.sha256(text.encode()).hexdigest(),
              'doctrine_utf8_bytes': len(text.encode()), 'source_hashes': originals}
    try:
        async with native_backend_fixture(root) as fixture:
            before = await ForkSessionHelper.run(ForkSessionRequest(str(package), str(before_session),
                str(fixture.project), str(root / 'before-fork')), cwd=fixture.project)
            after = await ForkSessionHelper.run(ForkSessionRequest(str(package), str(after_session),
                str(fixture.project), str(root / 'after-fork')), cwd=fixture.project)
            assert saved_copies(Path(before.session_file), text) == before_count
            assert before_count > 1
            fixture.session = Path(after.session_file)
            async with fixture.open_owner(runtime_enabled=True) as (owner, name):
                thread = owner._comms.registry.require(name)
                backend = owner.turns.backend_for(name, thread)
                before_query = digest(fixture.session)
                preview = await backend.inspect(thread, AgentCommsInspectContext())
                native_count = sum(segment.public_text().count(text) for segment in preview.segments)
                core = TurnContext.for_inspection(owner._comms, thread, now=time.time())
                core_count = core.render().text.count(text)
                after_count = native_count + core_count
                assert after_count == 1
                assert saved_copies(fixture.session, text) == 0
                assert digest(fixture.session) == before_query
                assert not stored_inputs(fixture.root).rows
                assert fixture.provider.posts == 0
                assert {str(path): digest(path) for path in (before_session, before_request, after_session)} == originals
                report.update(before=before_count, after=after_count,
                    before_scope='actual retained provider prompt, exact block verified in SDK fork history',
                    after_scope='installed SDK preview and next Core contributors; no future input',
                    before_fork=before.session_file, after_fork=after.session_file,
                    after_native_copies=native_count, after_core_copies=core_count,
                    fork_query_unchanged=True, originals_unchanged=True,
                    local_provider_posts=fixture.provider.posts)
            assert all(child.retired for child in fixture.children)
            report.update(state='PASS', native_children_retired=True)
    except BaseException as error:
        report.update(state='FAILED_NO_REPLAY', error=repr(error))
        raise
    finally:
        (root / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--before-session', required=True, type=Path)
    parser.add_argument('--before-request', required=True, type=Path)
    parser.add_argument('--after-session', required=True, type=Path)
    options = parser.parse_args()
    asyncio.run(run(options.root.resolve(), options.before_session.resolve(),
                    options.before_request.resolve(), options.after_session.resolve()))
