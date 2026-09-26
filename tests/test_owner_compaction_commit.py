"""Opt-in real native CAS + canonical Python owner + durable journal integration.

Only PI_COMPACTION_TEST_PACKAGE selects a disposable, patched package. No
provider calls or installed package edits. Normal unit suites skip this file.
"""

import json
import os
import selectors
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.compaction_journal import CompactionJournalError
from agent_comms.declarations import Goal, RelationViolationError, Thread, ThreadRegistry
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.owner_compaction_process import CompactionTransportUnknownError

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(
    not PACKAGE or os.name != "posix", reason="Disposable native opt-in"
)


@pytest.fixture
def native(tmp_path):
    script = """
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const moduleURL = pathToFileURL(join(process.argv[1], 'dist/core/session-manager.js'));
const {SessionManager} = await import(moduleURL);
const manager = SessionManager.create(process.argv[2], join(process.argv[2], 'sessions'));
const kept = manager.appendMessage({role:'user', content:'task', timestamp:1});
manager.appendMessage({role:'assistant', content:[{type:'text',text:'answer'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:2});
console.log(JSON.stringify(manager.captureCompactionWitness(kept)));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, PACKAGE, str(tmp_path)],
        capture_output=True,
        check=True,
        timeout=5,
    )
    witness = json.loads(result.stdout)
    registry = ThreadRegistry(tmp_path / "registry.json")
    owner = Thread(
        "owner",
        frozenset(),
        str(tmp_path),
        pid=os.getpid(),
        session_file=witness["sessionFile"],
        goal=Goal("task", "goal"),
    )
    registry.register(owner)
    owner, epoch = registry.live_owner_with_epoch("owner")
    owner, epoch = registry.claim_live_turn_with_epoch(owner, "turn", expected_epoch=epoch)
    bridge = OwnerCompactionCommit(tmp_path / "registry.json", Path(PACKAGE))
    return bridge, owner, epoch, witness


def entries(witness):
    return [json.loads(line) for line in Path(witness["sessionFile"]).read_text().splitlines()]


def test_positive_owner_validated_native_commit(native):
    bridge, owner, epoch, witness = native
    operation = bridge.commit(owner, epoch, witness, "retained summary", 42)
    assert operation.status == "committed", operation
    entry = entries(witness)[-1]
    assert entry["type"] == "compaction"
    assert entry["summary"] == "retained summary"
    assert entry["details"]["agentCommsCommit"]["commitId"] == operation.commit_id
    assert json.loads(operation.evidence_json)["entryId"] == entry["id"]


@pytest.mark.parametrize("mutation", ["stop", "heartbeat", "goal"])
def test_competing_registry_writer_waits_through_real_native_commit(native, monkeypatch, mutation):
    bridge, owner, epoch, witness = native
    call = bridge._call
    children = []
    script = """
import fcntl,sys
from pathlib import Path
from dataclasses import replace
from agent_comms.declarations import ThreadRegistry, Goal
root = Path(sys.argv[1])
with (root / '.registry.json.lock').open('ab') as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('blocked',flush=True)
    else:
        raise AssertionError('authority escaped before native write')
registry = ThreadRegistry(root / 'registry.json')
if sys.argv[2] == 'stop':
    registry.unregister('owner')
elif sys.argv[2] == 'heartbeat':
    registry.heartbeat('owner')
else:
    owner = registry.snapshot().threads['owner']
    registry.register(replace(owner,goal=Goal('new','new-goal')))
print('changed',flush=True)
"""

    def with_competitor(*args):
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(bridge.journal.path.parent), mutation],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        children.append(child)
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(5)
        assert child.stdout.readline() == b"blocked\n"
        return call(*args)

    monkeypatch.setattr(bridge, "_call", with_competitor)
    try:
        operation = bridge.commit(owner, epoch, witness, "summary", 42)
        assert operation.status == "committed"
        output, error = children[0].communicate(timeout=5)
        assert children[0].returncode == 0, error
        assert output == b"changed\n"
        assert (
            entries(witness)[-1]["details"]["agentCommsCommit"]["commitId"] == operation.commit_id
        )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()
            child.stderr.close()


def test_lost_native_result_never_replays_and_reconciles_exact_id(native, monkeypatch):
    bridge, owner, epoch, witness = native
    call = bridge._call

    def lose_result(*args):
        result = call(*args)
        assert result["status"] == "committed", result
        raise CompactionTransportUnknownError("lost result")

    monkeypatch.setattr(bridge, "_call", lose_result)
    operation = bridge.commit(owner, epoch, witness, "retained summary", 42)
    assert operation.status == "unknown"
    before = Path(witness["sessionFile"]).read_bytes()
    with pytest.raises(CompactionJournalError, match="never replay"):
        bridge.commit(owner, epoch, witness, "retained summary", 42)
    monkeypatch.setattr(bridge, "_call", call)
    resolved = bridge.reconcile(owner, epoch, operation.commit_id)
    assert resolved.status == "committed"
    assert Path(witness["sessionFile"]).read_bytes() == before
    assert len([entry for entry in entries(witness) if entry["type"] == "compaction"]) == 1


def test_outcome_persistence_failure_keeps_intent_and_requires_reconciliation(native, monkeypatch):
    bridge, owner, epoch, witness = native
    resolve = bridge.journal.resolve

    def fail_outcome(*args):
        raise OSError("outcome fsync unavailable")

    monkeypatch.setattr(bridge.journal, "resolve", fail_outcome)
    with pytest.raises(OSError, match="outcome fsync"):
        bridge.commit(owner, epoch, witness, "summary", 42)
    pending = bridge.journal.unresolved(witness["sessionFile"])
    assert len(pending) == 1 and pending[0].status == "intent"
    with pytest.raises(CompactionJournalError, match="never replay"):
        bridge.commit(owner, epoch, witness, "summary", 42)
    monkeypatch.setattr(bridge.journal, "resolve", resolve)
    assert bridge.reconcile(owner, epoch, pending[0].commit_id).status == "committed"


def test_missing_write_reconciles_absence_only_at_unchanged_revision(native, monkeypatch):
    bridge, owner, epoch, witness = native
    call = bridge._call

    def never_started(*args):
        raise CompactionTransportUnknownError("transport unavailable")

    monkeypatch.setattr(bridge, "_call", never_started)
    operation = bridge.commit(owner, epoch, witness, "summary", 42)
    assert operation.status == "unknown"
    monkeypatch.setattr(bridge, "_call", call)
    assert bridge.reconcile(owner, epoch, operation.commit_id).status == "aborted-no-write"
    with pytest.raises(CompactionJournalError):
        bridge.journal.begin(witness["sessionFile"], {}, commit_id=operation.commit_id)


def test_stopped_owner_refused_before_journal_or_native_mutation(native):
    bridge, owner, epoch, witness = native
    before = Path(witness["sessionFile"]).read_bytes()
    bridge.registry.unregister(owner.name)
    with pytest.raises(RelationViolationError):
        bridge.commit(owner, epoch, witness, "summary", 42)
    assert Path(witness["sessionFile"]).read_bytes() == before


def test_changed_goal_refused_before_native_mutation(native):
    bridge, owner, epoch, witness = native
    bridge.registry.register(replace(owner, goal=Goal("replacement", "new-goal")))
    with pytest.raises(RelationViolationError):
        bridge.commit(owner, epoch, witness, "summary", 42)
    assert entries(witness)[-1]["type"] == "message"


def test_forged_receipt_without_inherited_fd_never_writes(native):
    bridge, owner, epoch, witness = native
    before = Path(witness["sessionFile"]).read_bytes()
    result = subprocess.run(
        ["node", str(bridge.helper), str(bridge.package_dir), "-1"],
        input=json.dumps({"attestation": {"thread": owner.name}, "witness": witness}).encode(),
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["status"] == "unknown"
    assert Path(witness["sessionFile"]).read_bytes() == before


def test_native_stale_lock_requires_explicit_reconciliation(native):
    bridge, owner, epoch, witness = native
    lock = Path(witness["sessionFile"] + ".pr48-writer.lock")
    lock.write_text("fixture dead holder\n")
    operation = bridge.commit(owner, epoch, witness, "summary", 42)
    assert operation.status == "unknown"
    assert bridge.reconcile(owner, epoch, operation.commit_id).status == "unknown"
    lock.unlink()  # Explicit fixture operator decision; never automatic recovery.
    assert bridge.journal.get(operation.commit_id).status == "unknown"
    assert bridge.reconcile(owner, epoch, operation.commit_id).status == "aborted-no-write"


def test_owner_sigkill_after_native_write_before_journal_result(native, tmp_path):
    bridge, owner, epoch, witness = native
    script = """
import json,os,signal,sys
from pathlib import Path
from dataclasses import replace
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
bridge = OwnerCompactionCommit(Path(sys.argv[1]), Path(sys.argv[2]))
owner = bridge.registry.snapshot().threads['owner']
bridge.registry.unregister('owner')
bridge.registry.register(replace(owner,pid=os.getpid(),active_turn=None))
owner,epoch = bridge.registry.live_owner_with_epoch('owner')
owner,epoch = bridge.registry.claim_live_turn_with_epoch(owner,'crash-turn',expected_epoch=epoch)
call = bridge._call
def lose_result(*args):
    result = call(*args)
    assert result['status'] == 'committed', result
    print('native-durable-before-journal-result',flush=True)
    signal.pause()
bridge._call = lose_result
bridge.commit(owner,epoch,json.loads(sys.argv[3]),'crash summary',42)
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "registry.json"),
            PACKAGE,
            json.dumps(witness),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(5), "native mutation barrier not reached"
        assert child.stdout.readline() == b"native-durable-before-journal-result\n"
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
        pending = bridge.journal.unresolved(witness["sessionFile"])
        assert len(pending) == 1
        assert pending[0].status == "intent"
        before = Path(witness["sessionFile"]).read_bytes()
        # Explicit recovery under a fresh canonical owner/turn, no replay of
        # the stopped owner's request and no reuse of its epoch or receipt.
        bridge.registry.unregister("owner")
        bridge.registry.register(replace(owner, active_turn=None))
        recovered, epoch = bridge.registry.live_owner_with_epoch("owner")
        recovered, epoch = bridge.registry.claim_live_turn_with_epoch(
            recovered,
            "recovery-turn",
            expected_epoch=epoch,
        )
        result = bridge.reconcile(recovered, epoch, pending[0].commit_id)
        assert result.status == "committed"
        assert Path(witness["sessionFile"]).read_bytes() == before
        assert bridge.journal.unresolved(witness["sessionFile"]) == ()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()
        child.stderr.close()
