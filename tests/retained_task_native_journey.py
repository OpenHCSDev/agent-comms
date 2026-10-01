"""Three genuine selected commits on the existing native/ACP fixture, no replay."""

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from pytest import MonkeyPatch
from acp.schema import TextContentBlock
from agent_comms.acp_extension import CompactRequest, QueuePromptRequest, encode_request
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import SelectedSummarySource
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_origin import HumanInputOrigin
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent
from test_backend_native_lifecycle import native_backend


async def run(destination):
    import agent_comms

    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    checkout = Path(__file__).resolve().parents[1]
    installed = Path(agent_comms.__file__).resolve().parent
    if checkout in installed.parents:
        raise ValueError('Install the coherent Core wheel; source overlays are not acceptance')
    for original in (checkout / 'src/agent_comms').rglob('*.py'):
        assert (installed / original.relative_to(checkout / 'src/agent_comms')).read_bytes() == original.read_bytes()
    monkey = MonkeyPatch()
    fixture_generator = native_backend.__wrapped__(destination, monkey)
    fixture = await anext(fixture_generator)
    owner = None
    started = time.monotonic()
    receipt = {'state': 'RUNNING', 'core': str(installed), 'original_inputs_retried': 0,
               'public_changes': [], 'paid_provider_calls': 0, 'commits': [],
               'S4_model_recall': 'NOT_MEASURED: controlled provider is not a recall model',
               'installed_UI': False}

    def persist(phase):
        receipt['phase'] = phase
        receipt['elapsed_seconds'] = time.monotonic() - started
        (destination / 'receipt.json').write_text(json.dumps(receipt, indent=2))
        print(phase, flush=True)

    try:
        settings = fixture.config / 'settings.json'
        values = json.loads(settings.read_text())
        values['compaction'].update(reserveTokens=1024, keepRecentTokens=512)
        settings.write_text(json.dumps(values))
        owner = canonical_agent(Comms(fixture.root), agent_bin='pi',
            agent_args=['--model', 'response-local/fixture', '--thinking', 'off', '--offline',
                        '--no-extensions', '--no-skills', '--no-context-files',
                        '--no-prompt-templates', '--no-tools'], auto_wake=False, runtime_enabled=True)
        updates = []
        class Client:
            async def session_update(self, **value):
                updates.append(value)
        owner.on_connect(Client())
        thread = Thread('direct-human', frozenset(), str(fixture.project),
            process_identity=ProcessIdentity.capture(os.getpid()), session_file=str(fixture.session),
            model='response-local/fixture', thinking_level='off')
        owner._comms.registry.declare(thread)
        await owner.load_session(str(fixture.project), thread.name)
        inputs = InputDispositions(fixture.root / InputDispositions.filename)
        journal = CompactionJournal(fixture.root / 'compaction-commits.sqlite3')
        pin = None
        subject = None
        for round_number in range(1, 4):
            wording = f'S2_ORIGINAL_ROUND_{round_number} λ Never replay UNKNOWN.\n' + 'bounded retained source. ' * 1000
            origin = HumanInputOrigin.capture(owner._comms,
                owner._comms.registry.snapshot().admission_identity(thread.name))
            command = QueuePromptRequest(wording, origin=origin)
            persist(f'before_distinct_native_input_{round_number}')
            result = await owner.prompt(session_id=thread.name,
                prompt=[TextContentBlock(type='text', text=wording)], field_meta=encode_request(command))
            row = inputs.read().lookup('acp:' + command.input_id)
            assert row.has_started and row.source_text == wording
            if pin is None:
                subject = row
                pin = owner._comms.messaging.pin_input_constraint(thread.name, row.context_provenance(),
                                                                  worktree=thread.worktree)
                receipt['original_subject'] = FieldCodec.encode(subject)
                receipt['original_pin'] = FieldCodec.encode(pin)
            original_rows = inputs.path.read_bytes()
            before_calls = fixture.provider.posts
            persist(f'before_selected_compaction_{round_number}')
            await owner.prompt(session_id=thread.name, prompt=[TextContentBlock(type='text', text=' ')],
                               field_meta=encode_request(CompactRequest()))
            attempt, = tuple(a for a in journal.summaries.history(str(fixture.session))
                             if a.state.commit_id not in {r['commit_id'] for r in receipt['commits']})
            assert isinstance(attempt.state, ManualCommittedSummary)
            assert journal.operations.get(attempt.state.commit_id).state.committed
            captured = FieldCodec.decode(SelectedSummarySource, json.loads(attempt.source_json))
            snapshot = owner._comms.registry.snapshot()
            selected, = captured.retained.current_authored_sources(snapshot.require(thread.name), snapshot)
            assert selected == pin
            assert captured.retained.original_text_source(selected) == subject
            native_rows = [json.loads(line) for line in fixture.session.read_text().splitlines()]
            compacted = [r for r in native_rows if r['type'] == 'compaction']
            assert len(compacted) == round_number
            captured.retained.require_summary(compacted[-1]['summary'])
            assert inputs.path.read_bytes() == original_rows
            assert fixture.provider.posts > before_calls
            receipt['commits'].append({'round': round_number, 'commit_id': attempt.state.commit_id,
                'native_entry_id': compacted[-1]['id'], 'source_sha256': hashlib.sha256(attempt.source_json.encode()).hexdigest(),
                'original_input': row.key, 'selected_pin': FieldCodec.encode(selected.reference),
                'actual_summary_provider_calls': fixture.provider.posts - before_calls})
            persist(f'committed_native_round_{round_number}')
        origin = HumanInputOrigin.capture(owner._comms,
            owner._comms.registry.snapshot().admission_identity(thread.name))
        continuation = QueuePromptRequest('S2_DISTINCT_AFTER_THREE_COMPACTIONS', origin=origin)
        await owner.prompt(session_id=thread.name,
            prompt=[TextContentBlock(type='text', text=continuation.user_text)],
            field_meta=encode_request(continuation))
        assert inputs.read().lookup('acp:' + continuation.input_id).has_started
        assert len({r['native_entry_id'] for r in receipt['commits']}) == 3
        assert len({r['commit_id'] for r in receipt['commits']}) == 3
        assert 'S2_ORIGINAL_ROUND_1' in json.dumps(fixture.provider.requests[-1])
        receipt['state'] = 'INSTALLED_CORE_NATIVE_ACP_THREE_SELECTED_COMMITS_PASS'
        persist('distinct_native_continuation_after_three')
    except BaseException as error:
        receipt['state'] = 'FAILED_NO_REPLAY'
        receipt['error'] = repr(error)
        persist('terminal_failure')
        raise
    finally:
        if owner is not None:
            await owner.shutdown()
        await fixture_generator.aclose()
        monkey.undo()
        receipt['local_provider_posts'] = fixture.provider.posts
        (destination / 'original-provider-requests.json').write_text(json.dumps(fixture.provider.requests))
        receipt['retired_fixture_children'] = all(not child.alive() for child in fixture.children)
        persist('closed_original_fixture')


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]).absolute()))
