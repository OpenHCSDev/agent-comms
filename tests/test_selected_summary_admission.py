"""Provider-free exact selected terminal ACK → one native input-ID bind."""

import asyncio
import hashlib
import json
import os
import pickle
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import agent_events as ae
from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    CompactionJournalUnknownError,
    _ReturnedTerminalAck,
)
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import wire
from agent_comms.selected_summary_admission import (
    SelectedAdmissionIdentity,
    SelectedSummaryAdmission,
)
from agent_comms.store_files import _store_lock

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX journal and native input bind")


def _identity(
    session: str,
    *,
    text: str,
    key: str,
    turn: str,
    owner: str = "project",
    admission=1,
    original_text: str | None = None,
):
    digest = hashlib.sha256(text.encode()).hexdigest()
    original_digest = hashlib.sha256(
        (original_text if original_text is not None else text).encode()
    ).hexdigest()
    return SelectedAdmissionIdentity(
        owner_name=owner,
        owner_pid=os.getpid(),
        owner_created_at=(1.0).hex(),
        turn_id=turn,
        ingress_key=key,
        admission_generation=admission,
        correction_witness=f"{admission}:{digest}",
        input_sha256=digest,
        original_sha256=original_digest,
        reserved_revision=backend._session_revision(session),
        session_revision=backend._session_revision(session),
    )


def _source(identity):
    return {
        "source": {
            "ownerName": identity.owner_name,
            "ownerPid": identity.owner_pid,
            "ownerCreatedAt": identity.owner_created_at,
            "turnId": identity.turn_id,
            "ingressKey": identity.ingress_key,
            "admissionGeneration": identity.admission_generation,
            "correctionWitness": identity.correction_witness,
            "inputSha256": identity.input_sha256,
            "originalSha256": identity.original_sha256,
            "reservedRevision": json.loads(json.dumps(identity.reserved_revision)),
        },
        "selected": {"provider": "fake", "modelId": "fake"},
        "settings": {"reserveTokens": 100},
    }


@pytest.fixture
def case(tmp_path):
    comms = wire(tmp_path / "wire")
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
    key, text = "acp:" + "a" * 32, "original fake input"
    identity = _identity(str(session), text=text, key=key, turn="turn")
    dispositions = InputDispositions(comms.root)
    assert dispositions.record(
        key,
        seq=None,
        owner="project",
        admission=identity.admission_generation,
        target="project",
        text=text,
    )
    operation_id = journal.reserve_selected_summary(str(session), _source(identity))
    return comms, str(session), journal, operation_id, dispositions, identity, text


def _claim(case, token, *, identity=None, text=None, native_id=None):
    comms, session, _, _, dispositions, expected, original_text = case
    with _store_lock(comms._wire_lock_path):
        return token.consume_bound_original(
            wire_root=comms.root,
            session_file=session,
            identity=identity if identity is not None else expected,
            native_id=native_id or "b" * 32,
            sent_text=text if text is not None else original_text,
            dispositions=dispositions,
        )


@pytest.mark.parametrize("terminal", ["declined-prestart", "linked"])
def test_returned_ack_only_one_bound_original_and_no_status_bypass(case, terminal):
    comms, session, journal, operation_id, dispositions, identity, text = case
    if terminal == "linked":
        source = journal.selected_summary(operation_id).source_json
        commit_id = journal.begin(
            session,
            {
                "selectedSummaryOperationId": operation_id,
                "selectedSummarySourceDigest": hashlib.sha256(source.encode()).hexdigest(),
            },
        )
        journal.resolve(commit_id, "committed", {"fixture": "committed"})
        token = journal.link_selected_summary_commit(operation_id, commit_id, admission=identity)
    else:
        token = journal.decline_selected_summary_prestart(
            operation_id, "split_turn", admission=identity
        )
    assert token is not None
    with pytest.raises(TypeError, match="cannot cross"):
        pickle.dumps(token)
    assert not native_input_admitted(comms.root, session)
    assert _claim(case, token)
    row = dispositions.get(identity.ingress_key)
    assert row is not None and row["native_id"] == "b" * 32 and row["status"] == "unknown"
    assert not _claim(case, token, native_id="c" * 32)
    assert not native_input_admitted(comms.root, session)
    reopened = CompactionJournal(journal.path)
    with pytest.raises(CompactionJournalError):
        if terminal == "linked":
            reopened.link_selected_summary_commit(operation_id, commit_id, admission=identity)
        else:
            reopened.decline_selected_summary_prestart(
                operation_id, "split_turn", admission=identity
            )
    assert reopened.selected_summary(operation_id).status == terminal
    assert not native_input_admitted(comms.root, session)


@pytest.mark.parametrize(
    "change",
    [
        {"turn_id": "other"},
        {"owner_pid": 123456789},
        {"owner_created_at": (2.0).hex()},
        {"ingress_key": "acp:" + "f" * 32},
        {"admission_generation": 2},
        {"correction_witness": "changed"},
        {"input_sha256": "f" * 64},
        {"original_sha256": "f" * 64},
    ],
)
def test_mismatch_consumes_token_without_binding_or_recovery(case, change):
    comms, session, journal, operation_id, dispositions, identity, text = case
    token = journal.decline_selected_summary_prestart(
        operation_id, "unsupported", admission=identity
    )
    assert token is not None
    assert not _claim(case, token, identity=replace(identity, **change))
    assert not _claim(case, token)
    assert dispositions.get(identity.ingress_key)["native_id"] is None
    assert not native_input_admitted(comms.root, session)


def test_reserve_rejects_changed_saved_source_witness(tmp_path):
    comms = wire(tmp_path / "wire")
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    identity = _identity(str(session), text="input", key="acp:" + "a" * 32, turn="turn")
    journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
    session.write_text("{}\n{}\n")
    with pytest.raises(ValueError, match="revision changed"):
        journal.reserve_selected_summary(str(session), _source(identity))
    assert journal.blocking_selected_summary(str(session)) == ()


def test_reserve_refuses_wrong_durable_original(case):
    comms, session, journal, operation_id, dispositions, identity, text = case
    # A new session/input cannot replace the source text retained at admission.
    other = journal.path.parent / "other.jsonl"
    other.write_text("{}\n")
    key = "acp:" + "e" * 32
    assert dispositions.record(
        key, seq=None, owner="project", admission=1, target="project", text="actual original"
    )
    forged = _identity(
        str(other),
        text="injected replacement",
        key=key,
        turn="turn",
        original_text="injected replacement",
    )
    with pytest.raises(ValueError, match="durable original"):
        journal.reserve_selected_summary(str(other), _source(forged))
    assert not journal.blocking_selected_summary(str(other))


def test_link_cannot_mint_without_committed_native_source_digest(case):
    comms, session, journal, operation_id, dispositions, identity, text = case
    commit_id = journal.begin(session, {"selectedSummaryOperationId": operation_id})
    journal.resolve(commit_id, "committed", {"fixture": "committed"})
    with pytest.raises(CompactionJournalError, match="source digest"):
        journal.link_selected_summary_commit(operation_id, commit_id, admission=identity)
    assert journal.selected_summary(operation_id).status == "reserved"
    assert not native_input_admitted(comms.root, session)


def test_saved_source_drift_consumes_ack(case):
    comms, session, journal, operation_id, dispositions, identity, text = case
    token = journal.decline_selected_summary_prestart(
        operation_id, "split_turn", admission=identity
    )
    assert token is not None
    with open(session, "a", encoding="utf-8") as stream:
        stream.write("{}\n")
    assert not _claim(case, token)
    assert not _claim(case, token)
    assert dispositions.get(identity.ingress_key)["native_id"] is None
    assert not native_input_admitted(comms.root, session)


def test_forked_other_process_cannot_use_inherited_ack(case):
    comms, session, journal, operation_id, dispositions, identity, text = case
    token = journal.decline_selected_summary_prestart(
        operation_id, "split_turn", admission=identity
    )
    assert token is not None
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            os.write(write_fd, b"1" if _claim(case, token) else b"0")
        finally:
            os._exit(17)
    os.close(write_fd)
    assert os.read(read_fd, 1) == b"0"
    os.close(read_fd)
    assert os.waitpid(child, 0)[1] == 17 << 8
    assert dispositions.get(identity.ingress_key)["native_id"] is None
    assert not native_input_admitted(comms.root, session)


def test_changed_durable_original_after_reservation_refuses_burn(case):
    comms, session, journal, operation_id, dispositions, identity, text = case
    token = journal.decline_selected_summary_prestart(
        operation_id, "split_turn", admission=identity
    )
    assert token is not None
    with _store_lock(dispositions.path):
        rows = dispositions._read()
        rows[identity.ingress_key]["source_text"] = "different original"
        dispositions._write(rows)
    assert not _claim(case, token)
    assert not _claim(case, token)
    assert dispositions.get(identity.ingress_key)["native_id"] is None
    assert not native_input_admitted(comms.root, session)


def test_changed_journal_source_refuses_consumption(case):
    comms, session, journal, operation_id, dispositions, identity, text = case
    token = journal.decline_selected_summary_prestart(
        operation_id, "split_turn", admission=identity
    )
    assert token is not None
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "UPDATE selected_summary_attempts SET source_json = ? WHERE operation_id = ?",
            ('{"changed":true}', operation_id),
        )
    assert not _claim(case, token)
    assert not _claim(case, token)
    assert dispositions.get(identity.ingress_key)["native_id"] is None
    assert not native_input_admitted(comms.root, session)


def test_bind_fault_after_durable_unknown_never_replays(case, monkeypatch):
    comms, session, journal, operation_id, dispositions, identity, text = case
    token = journal.decline_selected_summary_prestart(
        operation_id, "split_turn", admission=identity
    )
    assert token is not None
    bind = dispositions.bind

    def bind_then_fail(*args, **kwargs):
        assert bind(*args, **kwargs)
        raise OSError("post-bind fault")

    monkeypatch.setattr(dispositions, "bind", bind_then_fail)
    assert not _claim(case, token)
    assert not _claim(case, token)
    assert dispositions.get(identity.ingress_key)["native_id"] == "b" * 32
    assert not native_input_admitted(comms.root, session)


@pytest.mark.parametrize("terminal", ["linked", "declined-prestart"])
def test_terminal_postcommit_fsync_fault_never_mints_ack(case, monkeypatch, terminal):
    comms, session, journal, operation_id, dispositions, identity, text = case
    if terminal == "linked":
        source = journal.selected_summary(operation_id).source_json
        commit_id = journal.begin(
            session,
            {
                "selectedSummaryOperationId": operation_id,
                "selectedSummarySourceDigest": hashlib.sha256(source.encode()).hexdigest(),
            },
        )
        journal.resolve(commit_id, "committed", {"fixture": "committed"})
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(CompactionJournalUnknownError):
        if terminal == "linked":
            journal.link_selected_summary_commit(operation_id, commit_id, admission=identity)
        else:
            journal.decline_selected_summary_prestart(
                operation_id, "split_turn", admission=identity
            )
    monkeypatch.undo()
    saved = journal.selected_summary(operation_id)
    assert saved.status == terminal
    with pytest.raises(CompactionJournalError, match="returned terminal fsync ACK"):
        SelectedSummaryAdmission._from_returned_ack(
            None,
            journal.path,
            session,
            operation_id,
            terminal,
            commit_id if terminal == "linked" else None,
            saved.source_json,
            identity,
        )
    assert not native_input_admitted(comms.root, session)
    assert dispositions.get(identity.ingress_key)["native_id"] is None


@pytest.mark.parametrize("terminal", ["declined-prestart", "linked"])
def test_private_status_only_transaction_cannot_issue_admission_ack(case, terminal):
    comms, session, journal, operation_id, dispositions, identity, text = case
    saved = journal.selected_summary(operation_id)
    scope = (str(journal.path), session, operation_id, terminal, None, saved.source_json)
    ack = [scope]
    # The exact predecessor bypass accepted these caller-supplied ACK arguments,
    # one raw SQL UPDATE, then issued an input-capable post-fsync receipt.
    with (
        pytest.raises(TypeError),
        journal._transaction(selected_ack=ack, selected_operation_id=operation_id),
    ):
        pass
    assert journal.selected_summary(operation_id).status == "reserved"
    with journal._transaction() as db:
        db.execute(
            "UPDATE selected_summary_attempts SET status = ? WHERE operation_id = ?",
            (terminal, operation_id),
        )
    assert journal.selected_summary(operation_id).status == terminal
    for forged in (scope, _ReturnedTerminalAck()):
        with pytest.raises(CompactionJournalError, match="returned terminal fsync ACK"):
            SelectedSummaryAdmission._from_returned_ack(
                forged,
                journal.path,
                session,
                operation_id,
                terminal,
                None,
                saved.source_json,
                identity,
            )
    with pytest.raises(CompactionJournalError):
        journal.decline_selected_summary_prestart(operation_id, "split_turn", admission=identity)
    assert not native_input_admitted(comms.root, session)
    assert dispositions.get(identity.ingress_key)["native_id"] is None


def test_success_without_admission_does_not_create_later_receipt(case):
    comms, session, journal, operation_id, dispositions, identity, text = case
    assert journal.decline_selected_summary_prestart(operation_id, "split_turn") is None
    saved = journal.selected_summary(operation_id)
    with pytest.raises(CompactionJournalError, match="returned terminal fsync ACK"):
        SelectedSummaryAdmission._from_returned_ack(
            None,
            journal.path,
            session,
            operation_id,
            "declined-prestart",
            None,
            saved.source_json,
            identity,
        )
    assert not native_input_admitted(comms.root, session)


@pytest.mark.parametrize("send", [False, True])
def test_process_death_before_or_after_fake_send_cannot_recreate_ack(tmp_path, send):
    comms = wire(tmp_path / "wire")
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    operation_id, key, text = "a" * 32, "acp:" + "a" * 32, "fake original"
    marker = tmp_path / "fake-stdin"
    # The child reserves, durably declines and mints its own ACK. In the
    # send case it binds its exact input ID and writes to a LOCAL marker only.
    script = """
import hashlib,json,os,sys
from pathlib import Path
from agent_comms import backend
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.store_files import _store_lock
from agent_comms.input_disposition import InputDispositions
from agent_comms.selected_summary_admission import SelectedAdmissionIdentity
root=Path(sys.argv[1]); session=sys.argv[2]; op=sys.argv[3]; key=sys.argv[4]; text=sys.argv[5]
digest=hashlib.sha256(text.encode()).hexdigest()
identity=SelectedAdmissionIdentity('project',os.getpid(),1.0.hex(),'turn',key,1,
    f'1:{digest}',digest,digest,backend._session_revision(session),backend._session_revision(session))
source={'source':{'ownerName':identity.owner_name,'ownerPid':identity.owner_pid,
    'ownerCreatedAt':identity.owner_created_at,'turnId':identity.turn_id,
    'ingressKey':identity.ingress_key,'admissionGeneration':1,
    'correctionWitness':identity.correction_witness,'inputSha256':digest,
    'originalSha256':digest,'reservedRevision':json.loads(json.dumps(identity.reserved_revision))},
    'selected':{'provider':'fake'},'settings':{'reserveTokens':100}}
j=CompactionJournal(root/'compaction-commits.sqlite3')
d=InputDispositions(root)
assert d.record(key,seq=None,owner='project',admission=1,target='project',text=text)
assert j.reserve_selected_summary(session,source,operation_id=op)==op
token=j.decline_selected_summary_prestart(op,'split_turn',admission=identity)
assert token is not None
if sys.argv[6]=='1':
    with _store_lock(root/'wire'):
        assert token.consume_bound_original(wire_root=root,session_file=session,identity=identity,
            native_id='b'*32,sent_text=text,dispositions=d)
        Path(sys.argv[7]).write_text('fake local stdin write')
os._exit(17)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(comms.root),
            str(session),
            operation_id,
            key,
            text,
            "1" if send else "0",
            str(marker),
        ],
        check=False,
        timeout=10,
    )
    assert result.returncode == 17
    assert marker.exists() == send
    journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
    assert journal.selected_summary(operation_id).status == "declined-prestart"
    assert not native_input_admitted(comms.root, str(session))
    row = InputDispositions(comms.root).get(key)
    assert row is not None and row["status"] == "unknown"
    assert (row["native_id"] == "b" * 32) == send


@pytest.mark.asyncio
async def test_acp_final_boundary_consumes_exact_ack_at_native_id_bind(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    agent.inputs.drain_tasks["project"].cancel()
    await asyncio.gather(agent.inputs.drain_tasks["project"], return_exceptions=True)
    session = tmp_path / "saved.jsonl"
    session.write_text("{}\n")
    comms.attach_session("project", str(session), pid=os.getpid())
    journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
    observed = []

    async def events(*args, **kwargs):
        thread = comms.registry.snapshot().threads["project"]
        key = agent.inputs.turn_original_input_keys["project"][0]
        text = args[2]
        digest = hashlib.sha256(text.encode()).hexdigest()
        identity = SelectedAdmissionIdentity(
            "project",
            thread.pid,
            float(thread.created_at).hex(),
            agent.turns.active_turns["project"],
            key,
            comms.registry.snapshot().admission_generations["project"],
            f"{comms.registry.snapshot().admission_generations['project']}:{digest}",
            digest,
            hashlib.sha256(agent.inputs.dispositions.get(key)["source_text"].encode()).hexdigest(),
            backend._session_revision(str(session)),
            backend._session_revision(str(session)),
        )
        operation_id = journal.reserve_selected_summary(str(session), _source(identity))
        token = journal.decline_selected_summary_prestart(
            operation_id, "split_turn", admission=identity
        )
        assert token is not None
        agent.inputs.selected_summary_admissions["project"] = token  # fake owner only; no producer
        with kwargs["send_boundary"](None, "c" * 32, text) as allowed:
            observed.append(allowed)
        with kwargs["send_boundary"](None, "d" * 32, text) as allowed:
            observed.append(allowed)
        yield ae.Done(ok=False, text="Provider-free fake")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.inputs.run_owned_input("project", "project", "original fake input")
        assert observed == [True, False]
        assert not native_input_admitted(comms.root, str(session))
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("terminal", ["linked", "declined-prestart"])
def test_native_start_retires_barrier_without_erasing_history_or_replaying_original(case, terminal):
    comms, session, journal, operation_id, dispositions, identity, text = case
    if terminal == "linked":
        source = journal.selected_summary(operation_id).source_json
        commit_id = journal.begin(
            session,
            {
                "selectedSummaryOperationId": operation_id,
                "selectedSummarySourceDigest": hashlib.sha256(source.encode()).hexdigest(),
            },
        )
        journal.resolve(commit_id, "committed", {"fixture": "committed"})
        token = journal.link_selected_summary_commit(operation_id, commit_id, admission=identity)
    else:
        token = journal.decline_selected_summary_prestart(
            operation_id, "unsupported", admission=identity
        )
    assert _claim(case, token)
    assert not native_input_admitted(comms.root, session), "bound UNKNOWN is not native start"
    assert dispositions.started(identity.ingress_key, turn_id="turn", native_id="b" * 32, text=text)
    reopened = CompactionJournal(journal.path)
    assert native_input_admitted(comms.root, session)
    assert reopened.blocking_selected_summary(session) == ()
    assert reopened.selected_summary(operation_id).status == terminal
    assert not _claim(case, token), "native start cannot replenish a consumed token"
    with reopened.ordinary_input_send_fence(Path(session)):
        pass
    with pytest.raises(ValueError, match="durable original input changed"):
        reopened.reserve_selected_summary(session, _source(identity))
    next_identity = _identity(session, text="Next original", key="acp:next", turn="next-turn")
    assert dispositions.record(
        next_identity.ingress_key,
        seq=None,
        owner=next_identity.owner_name,
        admission=next_identity.admission_generation,
        target=next_identity.owner_name,
        text="Next original",
    )
    next_id = reopened.reserve_selected_summary(session, _source(next_identity))
    assert [row.operation_id for row in reopened.blocking_selected_summary(session)] == [next_id]
    # A subsequent writer can bind ONLY the new reservation, despite historical
    # terminal rows remaining in the same table for audit and ID uniqueness.
    reopened.begin(session, {"selectedSummaryOperationId": next_id})
    assert not native_input_admitted(comms.root, session)


@pytest.mark.parametrize(
    "field,value",
    [
        ("turn_id", "other-turn"),
        ("owner", "other-owner"),
        ("target", "other-target"),
        ("admission", 2),
        ("sent_text", "other sent text"),
        ("source_text", "other original"),
        ("status", "unknown"),
        ("native_id", None),
    ],
)
def test_unrelated_or_uncertain_input_never_retires_selected_barrier(case, field, value):
    comms, session, journal, operation_id, dispositions, identity, text = case
    token = journal.decline_selected_summary_prestart(
        operation_id, "unsupported", admission=identity
    )
    assert _claim(case, token)
    assert dispositions.started(identity.ingress_key, turn_id="turn", native_id="b" * 32, text=text)
    rows = dispositions._read()
    rows[identity.ingress_key][field] = value
    dispositions._write(rows)
    assert not native_input_admitted(comms.root, session)
    with (
        pytest.raises(CompactionJournalError, match="blocks native input"),
        journal.ordinary_input_send_fence(Path(session)),
    ):
        raise AssertionError("stale native-start identity admitted")
