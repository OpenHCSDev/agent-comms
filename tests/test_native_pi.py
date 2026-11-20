"""Strict native proof-reader tests; actual RPC/provider exercise is separate."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from abc import abstractmethod
from dataclasses import replace
from pathlib import Path

import pytest
from agent_comms.execution.provider_records import ReportedText
from native_proof_cases import read_context_proof, read_proof_rows, write_proof_rows

from agent_comms.coordination.errors import IdentityConflict
from agent_comms.execution.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.native_entries import (
    NativeEntry,
    NativeEvidenceScope,
    NativeInputEvidenceRead,
)
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_input_proof import (
    NativeContextJournal,
    NativeContextProof,
)
from agent_comms.execution.native_pi import NativePiPromptRejected, NativePiRpcLaunch
from agent_comms.execution.native_session_files import NativeAgentSettings, NativeSessionFiles
from agent_comms.execution.native_session_reopen import PiSessionId
from agent_comms.execution.native_startup import NATIVE_STARTUP_POLICY
from agent_comms.execution.transcript_sources import NativeTranscript
from agent_comms.execution.pi_events import (
    ContextCommitted,
    FailedResponseOutcome,
    InputCommitted,
    PiEvent,
    Response,
)
from agent_comms.comms import wire
from agent_comms.execution.coding_child import CodingChild, ToollessMode
from agent_comms.execution.selected.session import SelectedSession
from agent_comms.execution.tracked_turn import TrackedTurnSession
from agent_comms.execution.turn_failure import (
    ModelRequestFailed,
    NativePiTerminalFailure,
)
from agent_comms.execution.turn_output import TurnOutput
from agent_comms.execution.turn_usage import UsageAccount
from agent_comms.execution.turn_watchdog import DEFAULT_MODEL_WAIT, ProgressWatchdog
from agent_comms.execution.pi_events import ContextCommitted, InputCommitted
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, InputId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.threads import NativeThread
from agent_comms.team.rule_deal import EmptyDealDelivery, SystemPromptDelivery

INPUT_ID = InputId('a' * 32)
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
    files = NativeSessionFiles.for_identity(NativeEvidenceScope.locate_session(str(session)),
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
    first = read_context_proof(files, INPUT_ID, session_entry_start=record.start)
    assert first == read_context_proof(files, INPUT_ID, session_entry_start=record.start)
    assert (first.session_id, first.session_entry_id, first.request_generation) == ('sid','entry',1)
    with pytest.raises(NativePiUnavailable):
        read_context_proof(files, InputId('d'*32), session_entry_start=record.start)


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
            for name in NativeContextJournal.triggers():
                db.execute(f'DROP TRIGGER "{name}"')


@pytest.mark.parametrize('damage', JournalDamage.members_with(JournalDamage), ids=lambda member: member.declared_name)
def test_corrupt_or_redirected_journal_cannot_assert_context(tmp_path, damage):
    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    damage().alter(files)
    with pytest.raises(NativePiUnavailable):
        read_context_proof(files, INPUT_ID, session_entry_start=record.start)


def test_native_journal_declaration_preserves_current_envelope_and_rejects_bad_facts(tmp_path):
    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    original = files.input_proof.read_bytes()
    row = read_proof_rows(files)[0]
    decoded = FieldCodec.decode(NativeContextJournal, row)
    assert FieldCodec.encode(decoded) == row
    assert decoded.at(session, session_entry_start=record.start) == read_context_proof(
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
    assert read_context_proof(files, INPUT_ID, session_entry_start=record.start) == decoded.at(
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
        read_context_proof(files, INPUT_ID, session_entry_start=record.start)
    session.write_text(data)
    session.parent.chmod(0o777)
    with pytest.raises(NativePiUnavailable):
        read_context_proof(files, INPUT_ID, session_entry_start=record.start)


def test_native_launch_arguments_and_identity_follow_delivery(tmp_path, native_runtime_package):
    prompt = tmp_path / "SYSTEM_PROMPT.md"
    prompt.write_text("small declared prompt")
    configuration = RestartEnvironment.inherit({})
    runtime = LaunchRuntime.running()
    empty = EmptyDealDelivery()
    supplied = SystemPromptDelivery(prompt)

    argv_empty, _ = NativePiRpcLaunch.bootstrap(
        native_runtime_package, ("--mode", "rpc"), tmp_path, {}, configuration, runtime, empty)
    argv_supplied, _ = NativePiRpcLaunch.bootstrap(
        native_runtime_package, ("--mode", "rpc"), tmp_path, {}, configuration, runtime, supplied)
    assert "--append-system-prompt" not in argv_empty
    assert argv_supplied[-2:] == ("--append-system-prompt", str(prompt.resolve()))

    session = SelectedSession(tmp_path / "sessions")
    launch_empty = NativePiRpcLaunch._build(
        native_runtime_package, (), tmp_path, {}, session, configuration, runtime, tmp_path, empty)
    launch_supplied = NativePiRpcLaunch._build(
        native_runtime_package, (), tmp_path, {}, session, configuration, runtime, tmp_path, supplied)
    assert launch_empty != launch_supplied

    comms = wire(tmp_path)
    thread = NativeThread("tracked", frozenset(), Checkout(tmp_path))
    comms.registry.register(thread)
    tracked = NativePiRpcLaunch.tracked(
        native_runtime_package,
        root=tmp_path,
        worktree=tmp_path,
        session=SelectedSession(tmp_path / "tracked"),
        model=SelectedModel("clm-local", "model"),
        selected_tool_mode=ToollessMode(
            CodingChild(
                comms=comms,
                thread=thread,
            )
        ),
        delivery=empty,
    )
    assert "--append-system-prompt" not in tracked.argv
    assert (tracked.env["CLM_CONTEXT_BUDGET"], tracked.env["CLM_CONTEXT_RESERVE"]) == ("28672", "2048")
    assert tracked.env["CLM_EDIT_RULE"] == "digest"
    provider_extension = str(native_runtime_package.path / "dist/clm_reference.mjs")
    assert tracked.argv.count(provider_extension) == 1


@pytest.mark.parametrize('provider,model', [('', 'model'),('provider',''),('--bad','model')])
def test_missing_native_model_fails_before_any_session_side_effect(tmp_path, provider, model, *, native_runtime_package):
    with pytest.raises(IdentityConflict):
        NativePiRpcLaunch.tracked(native_runtime_package, root=tmp_path, worktree=tmp_path,
            session=SelectedSession(tmp_path/'sessions'), model=SelectedModel(provider,model),
            selected_tool_mode=ToollessMode(CodingChild(
                comms=wire(tmp_path), thread=NativeThread('modelless', frozenset(), Checkout(tmp_path)))),
            delivery=EmptyDealDelivery())
    assert not (tmp_path/'sessions').exists()


def test_managed_launch_isolates_writable_settings_from_ambient_packages(
    tmp_path, monkeypatch, native_runtime_package,
):
    ambient = tmp_path / "ambient-agent"
    ambient.mkdir(mode=0o700)
    settings = ambient / "settings.json"
    original = '{"packages":["npm:mutable-package"]}\n'
    settings.write_text(original)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(ambient))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(ambient))

    session_directory = tmp_path / "managed-session"
    launch = NativePiRpcLaunch.managed(
        str(native_runtime_package.cli), (), package=native_runtime_package,
        worktree=tmp_path, root=tmp_path, owner=PROOF_OWNER,
        directory=session_directory, delivery=EmptyDealDelivery(),
    )

    private_agent = session_directory / NativeAgentSettings.directory_name
    assert launch.env["PI_CODING_AGENT_DIR"] == str(private_agent)
    assert launch.env["AGENT_COMMS_NATIVE_CONFIG_DIR"] == str(ambient)
    assert launch.configuration.agent_directory == private_agent
    assert launch.configuration.native_config == ambient
    assert private_agent.is_dir()
    assert settings.read_text() == original


@pytest.mark.parametrize(('stop_reason','detail'), [
    ('error','provider failed'),('length','Model output limit reached'),('aborted','provider failed'),
])
async def test_failed_stop_reason_records_failure_with_its_terminal(stop_reason, detail):
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
    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    context = NativeContextProof(INPUT_ID,PiSessionId('sid'),record.entry.id.require_present(),1,DIGEST,1_791_964_800_001,session,
                                 session_entry_start=record.start)
    failed = NativePiTerminalFailure('SECRET provider response including prompt data', context,
        SelectedModel('provider','model'), ModelRequestFailed('SECRET provider response including prompt data'))
    assert failed.public_message == 'provider/model: request failed.'
    assert 'SECRET' not in failed.public_message
    usage = NativePiTerminalFailure('Codex error: The usage limit has been reached', context,
        SelectedModel('openai-codex','gpt-6-sol'), ModelRequestFailed('Codex error: The usage limit has been reached'))
    assert 'The usage limit has been reached' in usage.public_message


def test_recorded_context_remains_verifiable_after_later_tool_rounds(tmp_path):
    files = _evidence(tmp_path)
    session = files.session
    _header, record = NativeTranscript(session).forward(0, session.stat().st_size)
    recorded = read_context_proof(files, INPUT_ID, session_entry_start=record.start)
    first = read_proof_rows(files)[0]
    later = {**first,'requestGeneration':2,'llmContextDigest':'d'*64}
    with sqlite3.connect(files.input_proof) as db:
        FieldCodec.decode(NativeContextJournal,later).insert(db)
    assert read_context_proof(files, INPUT_ID, session_entry_start=record.start).request_generation == 2
    assert read_context_proof(files, INPUT_ID, generations=range(1, 2), session_entry_start=record.start) == recorded
    with NativeEntry.open_evidence(session) as evidence:
        assert evidence.corroborates_context(recorded, input_id=INPUT_ID, files=files)
        assert not evidence.corroborates_context(recorded, input_id=InputId('e'*32), files=files)
    input_event = InputCommitted(input_id=INPUT_ID,session_id=PiSessionId('sid'),session_entry_id=record.entry.id.require_present(),session_entry_start=record.start)
    context_event = ContextCommitted.from_wire({key:value for key,value in first.items() if key != 'schema'})
    with NativeInputEvidenceRead.open(session) as evidence:
        assert evidence.verify_context(files, input_event,
                                       context_event.record_for(INPUT_ID, PiSessionId('sid'))) == recorded
        with pytest.raises(NativePiUnavailable):
            evidence.verify_context(files, input_event,
                replace(context_event, llm_context_digest='d'*64).record_for(INPUT_ID, PiSessionId('sid')))
    with pytest.raises(NativePiUnavailable):
        read_context_proof(files,INPUT_ID,generations=range(3, 4),session_entry_start=record.start)


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
    assert read_context_proof(files,INPUT_ID,generations=range(1, 2),session_entry_start=record.start).request_generation == 1


def test_proof_observation_rejects_file_replacement(tmp_path):
    files = _evidence(tmp_path)
    journal = files.input_proof
    with pytest.raises(NativePiUnavailable):
        with NativeContextJournal.open_evidence(files.input_proof) as db:
            assert NativeContextJournal.for_input(db,INPUT_ID).input_id == INPUT_ID
            changed = journal.with_name('replacement')
            changed.write_bytes(journal.read_bytes())
            changed.chmod(0o600)
            changed.replace(journal)


def test_a_rejected_prompt_carries_the_providers_own_words():
    """Pi's reason reaches the failure, not only the diagnostic file: on 2026-11-07 a thread
    could not resume after install because summarisation hit "The usage limit has been
    reached", and the turn reported only that something had been rejected."""
    response = Response(
        success=FailedResponseOutcome(),
        error=ReportedText("Summarization failed: Codex error: The usage limit has been reached"),
    )

    said = str(NativePiPromptRejected(response))

    assert "The usage limit has been reached" in said
    assert "rejected the tracked prompt" in said


def test_a_context_language_model_runs_with_owner_authorized_bash_alone(tmp_path):
    """The CLM reference harness gives the model exactly one action, bash, through which it
    edits its mirrored context. A clm-local model is offered the fleet's declared bash alone,
    on the coding transport its owner authorizes, whatever the input would grant; any other
    model keeps the input's mode."""
    from agent_comms.execution.channel_coding_tools import CodingToolMode
    from agent_comms.execution.context_language_model import ContextLanguageModelMode

    comms = wire(tmp_path)
    thread = NativeThread("clm", frozenset(), Checkout(tmp_path))
    comms.registry.register(thread)
    granted = ToollessMode(CodingChild(comms=comms, thread=thread))
    owner = object()
    mode = SelectedModel("clm-local", "model").provider_type.native_tool_mode(granted, owner)
    assert mode == ContextLanguageModelMode(owner)
    assert isinstance(mode, CodingToolMode)
    assert mode.allowed_tools() == ("bash",)
    assert SelectedModel("openai-codex", "gpt-5.6-sol").provider_type.native_tool_mode(granted, owner) is granted
