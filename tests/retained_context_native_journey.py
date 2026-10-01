"""Phase2 on the existing real S2/S5 native, ACP and Toad fixture."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from agent_comms.child_process import ProcessIdentity
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import SelectedSummarySource
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from retained_input_origin_observer import actual_s2_ingress, until


async def authored_context_operations(fixture, owner, source, fresh_session, fresh_project):
    """One human pin, genuine selected compaction, exported fresh native input.

    The original empty SDK fixture session is used for the receiving thread;
    copying/forking the compacted parent's history would mask a broken export.
    Source preparation only until the matched USER-ready installed pair exists.
    """
    output = fixture.root.parent / 'authored-context-journey'
    output.mkdir(mode=0o700)
    original_empty = Path(fresh_session).read_bytes()
    wording = 'S5_HUMAN_PIN_EXACT λ\nNever replay UNKNOWN; keep /original/source verbatim.'
    subject = owner._comms.messaging.send_user_message(
        '#team', wording, worktree=source.worktree)
    source_prefix = (fixture.root / 'bus.jsonl').read_bytes()

    async def command(*arguments):
        child = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'agent_comms.cli', '--root', str(fixture.root), *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        data, error = await child.communicate()
        assert child.returncode == 0, (data.decode(), error.decode())
        return json.loads(data)

    pin = await command('pin-constraint', source.name, '--source',
        f'{subject.seq}:{subject.message_id}', '--worktree', source.worktree)
    segment = owner._comms.bus.log.retained_context(source.name, owner._comms.registry)
    declarations = segment.retained.current_authored_sources(
        owner._comms.registry.require(source.name), owner._comms.registry.snapshot())
    declaration, = declarations
    assert declaration.reference == FieldCodec.decode(type(subject.reference), pin['pin'])
    assert declaration.task.require_human_constraint().subject == subject.reference
    assert segment.original_text_source(declaration) == subject
    assert wording not in declaration.body and owner._comms.registry.require(source.name).turn_lease is None
    (output / 'original-human-pin.json').write_text(json.dumps({
        'subject': FieldCodec.encode(subject), 'pin': FieldCodec.encode(declaration),
        'provenance': FieldCodec.encode(segment.provenance),
    }, ensure_ascii=False, indent=2))

    journal = CompactionJournal(fixture.root / 'compaction-commits.sqlite3')
    source_ui = output / 'source-ui'
    source_ui.mkdir(mode=0o700)
    async with actual_s2_ingress(owner, source, source_ui) as observer:
        async def compact_source():
            app = observer.observer.app
            view = app.selected_session.conversation
            assert view.agent.session_id == source.name
            await view.compact_context('Preserve the certified original human constraint verbatim.').wait()
            attempt, = journal.summaries.history(source.session_file)
            assert isinstance(attempt.state, ManualCommittedSummary)
            await until(observer.observer.pilot, lambda: 'Context compacted' in '\n'.join(
                strip.text for strip in app.screen._compositor.render_strips()))
            (source_ui / 'actual-committed-summary.svg').write_text(app.export_screenshot())
            return attempt

        attempt = await observer.run(compact_source())
    captured = FieldCodec.decode(SelectedSummarySource, json.loads(attempt.source_json))
    snapshot = owner._comms.registry.snapshot()
    committed_pin, = captured.retained.current_authored_sources(snapshot.require(source.name), snapshot)
    assert committed_pin == declaration
    assert captured.retained.original_text_source(committed_pin) == subject
    rows = [json.loads(line) for line in Path(source.session_file).read_text().splitlines()]
    compacted = [row for row in rows if row['type'] == 'compaction'][-1]
    captured.retained.require_summary(compacted['summary'])
    assert wording.replace('\n', '\\n') in compacted['summary']
    assert journal.operations.get(attempt.state.commit_id).state.committed

    destination = Path(fresh_project) / 'AGENTS.md'
    assert not destination.exists()
    exported = await command('export-retained', source.name, '--output', str(destination))
    assert exported['exported_messages'] == 1 and destination.read_text().count(wording) == 1
    artifact_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert exported['output_sha256'] == artifact_digest
    assert Path(fresh_session).read_bytes() == original_empty
    assert wording.replace('\n', '\\n') not in original_empty.decode()
    receiver = Thread('authored-context-fresh', frozenset(), str(fresh_project),
        process_identity=ProcessIdentity.capture(os.getpid()), session_file=str(fresh_session),
        model='response-local/fixture', thinking_level='off')
    owner._comms.registry.declare(receiver)
    await owner.load_session(str(fresh_project), receiver.name)
    await owner.turns.prepare_selected_session(receiver.name, receiver)
    before_request = fixture.provider.posts
    assert before_request == 1
    fresh_ui = output / 'fresh-ui'
    fresh_ui.mkdir(mode=0o700)
    async with actual_s2_ingress(owner, receiver, fresh_ui) as observer:
        reply = await observer.submit('S5_EXPORTED_CONTEXT_FIRST_INPUT')
    assert reply.has_started
    assert fixture.provider.posts == 2
    request = fixture.provider.requests[-1]
    assert any(message['role'] == 'system' and wording in message['content']
               for message in request['messages'])
    assert destination.read_bytes() and hashlib.sha256(destination.read_bytes()).hexdigest() == artifact_digest
    assert Path(fresh_session).read_bytes().startswith(original_empty)
    assert (fixture.root / 'bus.jsonl').read_bytes().startswith(source_prefix)
    receipt = {
        'scope': 'original human wire pin -> actual selected compaction -> atomic export -> fresh native ACP/Toad input',
        'source': FieldCodec.encode(subject.reference), 'pin': pin['pin'],
        'human_author': FieldCodec.encode(declaration.task.source_user),
        'selected_commit': attempt.state.commit_id, 'native_compaction_entry': compacted['id'],
        'fresh_native_input': FieldCodec.encode(reply), 'export': exported,
        'verbatim_source_intact': True, 'fresh_session_had_no_parent_summary': True,
        'local_provider_posts': fixture.provider.posts, 'paid_public_inputs': 0, 'original_inputs_retried': 0,
        'core': __import__('agent_comms').__file__, 'toad': __import__('toad').__file__,
    }
    (output / 'phase2-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
