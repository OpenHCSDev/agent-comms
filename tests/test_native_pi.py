"""Strict native proof-reader tests; actual RPC/provider exercise is separate."""
from __future__ import annotations

from abc import abstractmethod
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.native_arguments import SelectedModel
from agent_comms.native_pi import NativeContextJournal, NativeContextProof, NativePiRpcLaunch, NativePiTerminalFailure, NativePiUnavailable
from agent_comms.native_session_files import NativeSessionFiles
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.native_startup import NATIVE_STARTUP_POLICY
from agent_comms.native_transcript import NativeTranscript
from agent_comms.selected_actions import NoSelectedTools
from agent_comms.selected_session import SelectedSession
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.tracked_turn import TrackedTurnSession
from native_proof_cases import read_proof_rows, write_proof_rows

INPUT_ID = 'a' * 32
DIGEST = 'b' * 64
PROOF_OWNER = RecordedThreadIncarnation('proof-case', 1.0)
pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='native UID checks require POSIX')


def _evidence(tmp_path: Path) -> NativeSessionFiles:
    sessions = tmp_path / 'sessions'
    sessions.mkdir(mode=0o700)
    session = sessions / 'test.jsonl'
    entries = [
        {'type':'session','timestamp':'2026-10-01T00:00:00.000Z','id':'sid','version':3},
        {'type':'message','timestamp':'2026-10-01T00:00:00.000Z','id':'entry',
         'message':{'role':'user','inputId':INPUT_ID,'inputDigest':DIGEST,
                    'content':[{'type':'text','text':'separate from its identity'}]}},
    ]
    session.write_text(''.join(json.dumps(row)+'\n' for row in entries))
    session.chmod(0o600)
    files = NativeSessionFiles.for_identity(NativeSessionIdentity.locate(str(session)),
                                            root=tmp_path, owner=PROOF_OWNER)
    write_proof_rows(files, [{
        'schema':2,'type':'context_committed','sessionId':'sid','inputId':INPUT_ID,
        'sessionEntryId':'entry','requestGeneration':1,'llmContextDigest':'c'*64,
        'committedAtMs':1_791_964_800_001,
    }])
    return files


def test_read_only_journal_parser_is_not_recovery_authority(tmp_path):
    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    first = NativeContextProof.read_evidence(files, INPUT_ID, session_entry_start=record.start)
    assert first == NativeContextProof.read_evidence(files, INPUT_ID, session_entry_start=record.start)
    assert (first.session_id, first.session_entry_id, first.request_generation) == ('sid','entry',1)
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(files, 'd'*32, session_entry_start=record.start)


class JournalDamage(DeclaredFamily, affix='Damage'):
    @abstractmethod
    def alter(self, files: NativeSessionFiles) -> None: ...


class BytesDamage(JournalDamage):
    def alter(self, files):
        files.input_proof.write_bytes(self.changed(files.input_proof.read_bytes()))

    @abstractmethod
    def changed(self, original: bytes) -> bytes: ...


class MalformedDamage(BytesDamage):
    def changed(self, original):
        return b'not a SQLite database'


class TruncatedDamage(BytesDamage):
    def changed(self, original):
        return original[:-4096]


class WorldReadableDamage(JournalDamage):
    def alter(self, files):
        files.input_proof.chmod(0o644)


class RowDamage(JournalDamage):
    def alter(self, files):
        rows = read_proof_rows(files)
        self.change(rows[0])
        write_proof_rows(files, rows)

    @abstractmethod
    def change(self, row: dict[str, object]) -> None: ...


class WrongEntryDamage(RowDamage):
    def change(self, row):
        row['sessionEntryId'] = 'other'


class WrongSessionDamage(RowDamage):
    def change(self, row):
        row['sessionId'] = 'other'


class LinkDamage(JournalDamage):
    def alter(self, files):
        original = files.root / 'replacement'
        files.input_proof.rename(original)
        self.link(original, files.input_proof)

    @abstractmethod
    def link(self, original: Path, selected: Path) -> None: ...


class SymlinkDamage(LinkDamage):
    def link(self, original, selected):
        selected.symlink_to(original)


class HardlinkDamage(LinkDamage):
    def link(self, original, selected):
        os.link(original, selected)


class RemovedConstraintDamage(JournalDamage):
    def alter(self, files):
        with sqlite3.connect(files.input_proof) as db:
            db.execute(f'DROP TRIGGER {NativeContextJournal.declared_name}_append')


@pytest.mark.parametrize('damage', JournalDamage.members_with(JournalDamage), ids=lambda member: member.declared_name)
def test_corrupt_or_redirected_journal_cannot_assert_context(tmp_path, damage):
    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    damage().alter(files)
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(files, INPUT_ID, session_entry_start=record.start)


def test_native_journal_declaration_preserves_current_envelope_and_rejects_bad_facts(tmp_path):
    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    original = files.input_proof.read_bytes()
    row = read_proof_rows(files)[0]
    decoded = FieldCodec.decode(NativeContextJournal, row)
    assert FieldCodec.encode(decoded) == row
    assert decoded.at(session, session_entry_start=record.start) == NativeContextProof.read_evidence(
        files, INPUT_ID, session_entry_start=record.start)
    for changed in (
        {**row,'schema':True}, {**row,'schema':1}, {**row,'committedAtMs':0},
        {**row,'type':'input_committed'}, {**row,'requestGeneration':True},
        {**row,'requestGeneration':0}, {**row,'inputId':'not-an-input'},
        {**row,'llmContextDigest':'not-a-digest'}, {**row,'extra':'not-native'},
        {key:value for key,value in row.items() if key != 'schema'},
    ):
        with pytest.raises((ValueError, TypeError)):
            FieldCodec.decode(NativeContextJournal, changed)
    assert NativeContextProof.read_evidence(files, INPUT_ID, session_entry_start=record.start) == decoded.at(
        session, session_entry_start=record.start)
    assert files.input_proof.read_bytes() == original


def test_proof_reader_rejects_session_id_duplicate_or_untrusted_ancestor(tmp_path):
    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    data = session.read_text()
    user = json.loads(data.splitlines()[1])
    session.write_text(data+json.dumps({**user,'id':'another'})+'\n')
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(files, INPUT_ID, session_entry_start=record.start)
    session.write_text(data)
    session.parent.chmod(0o777)
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(files, INPUT_ID, session_entry_start=record.start)


@pytest.mark.parametrize('provider,model', [('', 'model'),('provider',''),('--bad','model')])
def test_missing_native_model_fails_before_any_session_side_effect(tmp_path, provider, model, *, native_runtime_package):
    with pytest.raises(IdentityConflict):
        NativePiRpcLaunch.tracked(native_runtime_package, root=tmp_path, worktree=tmp_path,
            session=SelectedSession(tmp_path/'sessions'), model=SelectedModel(provider,model),
            selected_tool_mode=NoSelectedTools())
    assert not (tmp_path/'sessions').exists()


@pytest.mark.parametrize(('stop_reason','detail'), [
    ('error','provider failed'),('length','Model output limit reached'),('aborted','provider failed'),
])
async def test_failed_stop_reason_records_failure_with_its_terminal(stop_reason, detail):
    from agent_comms.pi_events import PiEvent
    from agent_comms.turn_failure import ModelRequestFailed
    from agent_comms.turn_output import TurnOutput
    from agent_comms.turn_usage import UsageAccount
    from agent_comms.turn_watchdog import DEFAULT_MODEL_WAIT, ProgressWatchdog

    event = PiEvent.from_wire({'type':'message_end','message':{
        'role':'assistant','content':[],'stopReason':stop_reason,
        'errorMessage':detail if stop_reason != 'length' else None,
    }})
    turn = object.__new__(TrackedTurnSession)
    output = turn.output = TurnOutput()
    turn.usage, turn.explicit_interrupt = UsageAccount(), False
    turn.watchdog = ProgressWatchdog(DEFAULT_MODEL_WAIT,1,1,NATIVE_STARTUP_POLICY)
    [update async for update in event.message.stop_reason.apply(turn,event.message)]
    assert output.declared_failure == ModelRequestFailed(detail)
    with pytest.raises(NativePiTerminalFailure) as caught:
        output.raise_if_failed(None, SelectedModel('provider','model'))
    assert caught.value.failure == output.declared_failure


def test_provider_failure_notice_does_not_broadcast_untrusted_error_body(tmp_path):
    from agent_comms.turn_failure import ModelRequestFailed

    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    context = NativeContextProof(INPUT_ID,'sid','entry',1,DIGEST,1_791_964_800_001,session,
                                 session_entry_start=record.start)
    failed = NativePiTerminalFailure('SECRET provider response including prompt data', context,
        SelectedModel('provider','model'), ModelRequestFailed('SECRET provider response including prompt data'))
    assert failed.public_message == 'provider/model: request failed.'
    assert 'SECRET' not in failed.public_message
    usage = NativePiTerminalFailure('Codex error: The usage limit has been reached', context,
        SelectedModel('openai-codex','gpt-6-sol'), ModelRequestFailed('Codex error: The usage limit has been reached'))
    assert 'The usage limit has been reached' in usage.public_message


def test_recorded_context_remains_verifiable_after_later_tool_rounds(tmp_path):
    from dataclasses import replace
    from agent_comms.native_pi import _verify_context
    from agent_comms.pi_events import ContextCommitted, InputCommitted

    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    recorded = NativeContextProof.read_evidence(files, INPUT_ID, session_entry_start=record.start)
    first = read_proof_rows(files)[0]
    later = {**first,'requestGeneration':2,'llmContextDigest':'d'*64}
    with sqlite3.connect(files.input_proof) as db:
        FieldCodec.decode(NativeContextJournal,later).insert(db)
    assert NativeContextProof.read_evidence(files, INPUT_ID, session_entry_start=record.start).request_generation == 2
    assert NativeContextProof.read_evidence(files, INPUT_ID, request_generation=1, session_entry_start=record.start) == recorded
    assert recorded.corroborates_input(INPUT_ID, files=files)
    assert not recorded.corroborates_input('e'*32, files=files)
    input_event = InputCommitted(input_id=INPUT_ID,session_id='sid',session_entry_id='entry',session_entry_start=record.start)
    context_event = ContextCommitted.from_wire({key:value for key,value in first.items() if key != 'schema'})
    assert _verify_context(files,INPUT_ID,'sid',input_event,context_event) == recorded
    with pytest.raises(NativePiUnavailable):
        _verify_context(files,INPUT_ID,'sid',input_event,replace(context_event,llm_context_digest='d'*64))
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(files,INPUT_ID,request_generation=3,session_entry_start=record.start)


def test_native_proof_database_enforces_generation_and_digest_without_scanning(tmp_path):
    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    first = read_proof_rows(files)[0]
    with sqlite3.connect(files.input_proof) as db:
        FieldCodec.decode(NativeContextJournal,{**first,'requestGeneration':2}).insert(db)
        for invalid in (first, {**first,'inputId':'d'*32},
                        {**first,'inputId':'d'*32,'requestGeneration':2,'llmContextDigest':'e'*64}):
            with pytest.raises(sqlite3.IntegrityError):
                FieldCodec.decode(NativeContextJournal,invalid).insert(db)
    assert NativeContextProof.read_evidence(files,INPUT_ID,request_generation=1,session_entry_start=record.start).request_generation == 1


def test_proof_observation_rejects_file_replacement(tmp_path):
    files = _evidence(tmp_path)
    journal = files.input_proof
    with pytest.raises(NativePiUnavailable):
        with NativeContextJournal.open_evidence(files) as db:
            assert NativeContextJournal.for_input(db,INPUT_ID).input_id == INPUT_ID
            changed = journal.with_name('replacement')
            changed.write_bytes(journal.read_bytes())
            changed.chmod(0o600)
            changed.replace(journal)
