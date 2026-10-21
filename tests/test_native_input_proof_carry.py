"""Actual SQLite resources move inside the copied root, without copying proof bytes."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.participants import Participants
from agent_comms.errors import RelationViolationError
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.locked_store import SQLiteFile
from agent_comms.native_claim_epoch import RecordedNativeClaim
from agent_comms.native_input_record import TriageNativeExecution, UnrecordedNativeColumn
from agent_comms.native_pi import NativeContextJournal, NativeContextProof
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_session_files import NativeSessionFiles
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.thread_identity import RecordedThreadIncarnation
from cutover.carry_native_input_proofs import NativeInputProofs

OWNER = RecordedThreadIncarnation('proof-owner', 1.0)


def source(root: Path, mode='DELETE', owners=(OWNER,)):
    root.mkdir(mode=0o700)
    identity = create_fresh_private_session(root, worktree=root)
    with identity.path.open('ab') as transcript:
        transcript.write((json.dumps({
            'type':'message','timestamp':'2026-10-01T00:00:00.000Z','id':'entry',
            'message':{'role':'user','inputId':'a'*32,'inputDigest':'b'*64,
                       'content':[{'type':'text','text':'retained source'}]},
        })+'\n').encode())
    with closing(sqlite3.connect(CoordinationStore.path_in(root))) as db:
        Participants.create(db)
        NativeRuntimeInput.create(db)
        for ordinal, owner in enumerate(owners):
            Participants(participant_lookup=owner.lookup, display_name=owner.name, committed=True).insert(db)
            NativeRuntimeInput(
                input_id=f'{ordinal+1:032x}', stage=TriageNativeExecution,
                execution_id=UnrecordedNativeColumn(), attempt_ordinal=UnrecordedNativeColumn(),
                owner_lookup=owner.lookup,
                owner_thread=owner.name, owner_generation=1, owner_token_digest='e'*64,
                sent_owner_claimed_generation=RecordedNativeClaim(1), session_id=identity.session_id,
                # A copied record names the original; the carry never follows it.
                session_file='/original/bus/sessions/'+identity.path.name,
            ).insert(db)
        db.commit()
    CoordinationStore.path_in(root).chmod(0o600)
    legacy = identity.path.with_name(identity.path.name+'.input-proof')
    # A real abruptly closed writer leaves WAL/SHM without a live borrower.
    completed = subprocess.run([sys.executable, '-c', '''
import os,sqlite3,sys
from agent_comms.native_pi import NativeContextJournal
path,sid,mode=sys.argv[1:]
os.close(os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600))
db=sqlite3.connect(path)
db.execute('PRAGMA journal_mode='+mode)
NativeContextJournal.create(db)
NativeContextJournal(input_id='a'*32, session_id=sid, session_entry_id='entry',
 request_generation=1,llm_context_digest='c'*64,committed_at_ms=1791964800001,
 schema=2,type='context_committed').insert(db)
db.commit()
os.chmod(path,0o600)
os._exit(0)
''', str(legacy),identity.session_id,mode], timeout=15, capture_output=True)
    assert completed.returncode == 0, completed.stderr.decode()
    return NativeSessionFiles.for_identity(identity,root=root,owner=OWNER), legacy


@pytest.mark.parametrize('mode', ['DELETE','WAL'])
def test_move_keeps_inode_rows_and_all_sqlite_companions(tmp_path, mode):
    root = tmp_path/'copy'
    files, legacy = source(root, mode)
    before = {path: path.stat().st_ino for path in SQLiteFile.files(legacy) if path.exists()}
    if mode == 'WAL':
        assert len(before) == 3
    transcript = files.session.read_bytes()
    assert NativeInputProofs.carry(root,apply=False).changed == 1
    assert all(path.exists() for path in before)
    moved = NativeInputProofs.carry(root,apply=True)
    assert moved.detail['proofs'] == 1
    assert files.input_proof.stat().st_ino == before[legacy]
    assert not any(path.exists() for path in before)
    assert files.session.read_bytes() == transcript
    with NativeContextJournal.open_evidence(files) as db:
        rows = NativeContextJournal.select(db)
        assert len(rows)==1 and rows[0].session_id==files.session_id
    assert NativeInputProofs.carry(root,apply=True).changed == 0


def test_scope_and_rename_follow_native_identity_not_filename(tmp_path):
    root=tmp_path/'copy'
    files,legacy=source(root)
    NativeInputProofs.carry(root,apply=True)
    with files.session.open('rb') as transcript:
        transcript.readline()
        start=transcript.tell()
    renamed=files.session.with_name('renamed.jsonl')
    files.session.rename(renamed)
    current=NativeSessionFiles.for_identity(NativeSessionIdentity.locate(str(renamed)),root=root,owner=OWNER)
    assert current.input_proof==files.input_proof
    observed=NativeContextProof.read_evidence(current,'a'*32,session_entry_start=start)
    assert observed.session_id==files.session_id and observed.input_id=='a'*32
    other_owner=NativeSessionFiles.for_identity(current,root=root,
        owner=RecordedThreadIncarnation('other',2.0))
    other_bus=NativeSessionFiles.for_identity(current,root=tmp_path/'other-bus',owner=OWNER)
    assert current.input_proof!=other_owner.input_proof
    assert current.input_proof!=other_bus.input_proof


@pytest.mark.parametrize('owners', [(),(OWNER,RecordedThreadIncarnation('other',2.0))])
def test_unknown_or_ambiguous_owner_refuses_before_any_move(tmp_path, owners):
    root=tmp_path/'copy'
    files,legacy=source(root,owners=owners)
    before=legacy.read_bytes()
    with pytest.raises(RelationViolationError,match='no unique recorded owner'):
        NativeInputProofs.carry(root,apply=True)
    assert legacy.read_bytes()==before and not files.input_proof.exists()


def test_collision_never_overwrites_either_proof(tmp_path):
    root=tmp_path/'copy'
    files,legacy=source(root)
    files.input_proof.parent.mkdir(parents=True,mode=0o700)
    files.input_proof.write_bytes(b'other proof')
    before=legacy.read_bytes()
    with pytest.raises(RelationViolationError,match='already exists'):
        NativeInputProofs.carry(root,apply=True)
    assert legacy.read_bytes()==before and files.input_proof.read_bytes()==b'other proof'


@pytest.mark.parametrize('redirect', ['proof','transcript','coordinator'])
def test_copy_alias_never_changes_the_original(tmp_path,redirect):
    root=tmp_path/'copy'
    files,legacy=source(root)
    paths={'proof':legacy,'transcript':files.session,'coordinator':CoordinationStore.path_in(root)}
    selected=paths[redirect]
    original=tmp_path/'original'
    selected.rename(original)
    selected.symlink_to(original)
    digest=hashlib.sha256(original.read_bytes()).digest()
    with pytest.raises(RelationViolationError,match='unconfined'):
        NativeInputProofs.carry(root,apply=True)
    assert hashlib.sha256(original.read_bytes()).digest()==digest
    assert not files.input_proof.exists()


def test_unrecognized_companion_is_preserved_and_refused(tmp_path):
    root=tmp_path/'copy'
    files,legacy=source(root)
    staged=legacy.with_name(legacy.name+'.initializing-123')
    staged.write_bytes(b'unresolved publication')
    with pytest.raises(RelationViolationError,match='unrecognized companions'):
        NativeInputProofs.carry(root,apply=True)
    assert staged.read_bytes()==b'unresolved publication' and legacy.exists()
    assert not files.input_proof.exists()


def external_source(tmp_path, name):
    bus = tmp_path / (name + '-bus')
    files, legacy = source(bus)
    journals = tmp_path / (name + '-journals')
    journals.mkdir(mode=0o700)
    transcript = journals / files.session.name
    files.session.rename(transcript)
    external = journals / legacy.name
    legacy.rename(external)
    files = NativeSessionFiles.for_identity(
        NativeSessionIdentity.locate(str(transcript)), root=bus, owner=OWNER,
    )
    return files, external, journals


def create_target(files, row):
    files.prepare_private_directory(files.input_proof.parent)
    files.input_proof.touch(mode=0o600)
    with closing(sqlite3.connect(files.input_proof)) as db:
        NativeContextJournal.create_schema(db)
        row.insert(db)
        db.commit()


def test_external_history_merge_is_atomic_and_preserves_originals(tmp_path):
    files, legacy, journals = external_source(tmp_path, 'missing')
    # Different journal lifetimes can reuse a generation for different inputs;
    # immutable evidence is keyed by the declared input/generation pair.
    create_target(files, NativeContextJournal(
        input_id='f'*32, session_id=files.session_id, session_entry_id='new-entry',
        request_generation=1, llm_context_digest='e'*64, committed_at_ms=1791964800002,
        schema=2, type='context_committed',
    ))
    original = hashlib.sha256(legacy.read_bytes()).digest()
    inode = files.input_proof.stat().st_ino
    plan, = NativeInputProofs.histories(files.root, journals)
    preview = plan.inspect(journals)
    assert (preview.difference.missing_rows, preview.difference.conflicting_keys) == (1, 0)
    assert plan.apply(journals).inserted_rows == 1
    assert files.input_proof.stat().st_ino == inode
    with NativeContextJournal.open_evidence(files) as db:
        assert NativeContextJournal.count(db) == 2
        assert not NativeContextJournal.schema_differs(db)
    # A normal writer still uses the unmodified append-only contract afterward.
    with closing(sqlite3.connect(files.input_proof)) as db:
        NativeContextJournal(
            input_id='d'*32, session_id=files.session_id, session_entry_id='later-entry',
            request_generation=2, llm_context_digest='d'*64, committed_at_ms=1791964800003,
            schema=2, type='context_committed',
        ).insert(db)
        db.commit()
    target_hash = hashlib.sha256(files.input_proof.read_bytes()).digest()
    assert plan.apply(journals).inserted_rows == 0
    assert hashlib.sha256(files.input_proof.read_bytes()).digest() == target_hash
    assert hashlib.sha256(legacy.read_bytes()).digest() == original

    conflict, old, conflict_root = external_source(tmp_path, 'conflict')
    with closing(sqlite3.connect(old)) as db:
        NativeContextJournal(
            input_id='b'*32, session_id=conflict.session_id, session_entry_id='another-entry',
            request_generation=2, llm_context_digest='d'*64, committed_at_ms=1791964800003,
            schema=2, type='context_committed',
        ).insert(db)
        db.commit()
    create_target(conflict, NativeContextJournal(
        input_id='a'*32, session_id=conflict.session_id, session_entry_id='entry',
        request_generation=1, llm_context_digest='e'*64, committed_at_ms=1791964800001,
        schema=2, type='context_committed',
    ))
    old_hash = hashlib.sha256(old.read_bytes()).digest()
    target_hash = hashlib.sha256(conflict.input_proof.read_bytes()).digest()
    refused, = NativeInputProofs.histories(conflict.root, conflict_root)
    preview = refused.inspect(conflict_root)
    assert (preview.difference.missing_rows, preview.difference.conflicting_keys) == (1, 1)
    with pytest.raises(RelationViolationError, match='conflicting keys'):
        refused.apply(conflict_root)
    assert hashlib.sha256(conflict.input_proof.read_bytes()).digest() == target_hash
    assert hashlib.sha256(old.read_bytes()).digest() == old_hash
    with NativeContextJournal.open_evidence(conflict) as db:
        assert NativeContextJournal.count(db) == 1

    skipped, preserved, skipped_root = external_source(tmp_path, 'missing-target')
    preserved_hash = hashlib.sha256(preserved.read_bytes()).digest()
    untouched, = NativeInputProofs.histories(skipped.root, skipped_root)
    assert untouched.apply(skipped_root).inserted_rows == 0
    assert not skipped.input_proof.exists()
    assert hashlib.sha256(preserved.read_bytes()).digest() == preserved_hash
