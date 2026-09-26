"""Opt-in real native CAS + canonical Python owner + durable journal integration.

Only PI_COMPACTION_TEST_PACKAGE selects a disposable, patched package. No
provider calls or installed package edits. Normal unit suites skip this file.
"""

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from agent_comms.compaction_journal import CompactionJournalError, CompactionJournalUnknownError
from agent_comms.declarations import Goal, RelationViolationError, Thread, ThreadRegistry
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import Comms
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.owner_compaction_process import CompactionTransportUnknownError
from agent_comms.session_fence import SessionWriterBusyError, session_writer_fence

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(
    not PACKAGE or sys.platform != "linux", reason="Disposable Linux native opt-in"
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
    # Capture once BEFORE each test's summary/invalidations, never at commit.
    source = bridge.capture_source(owner, epoch, witness)
    bridge.commit = partial(bridge.commit, source=source)
    return bridge, owner, epoch, witness


def entries(witness):
    return [json.loads(line) for line in Path(witness["sessionFile"]).read_text().splitlines()]


def test_compaction_child_refuses_external_helper_before_execution(native, tmp_path):
    bridge, _, _, _ = native
    marker = tmp_path / "external-helper-executed"
    helper = tmp_path / "external-helper.mjs"
    helper.write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(marker))}, 'unsafe');"
    )
    bridge.helper = helper  # A trusted test's attempted override still cannot escape the fence.
    with (
        (tmp_path / "authority").open("w") as authority,
        pytest.raises(CompactionTransportUnknownError, match="Unparseable native outcome"),
    ):
        bridge._call(authority.fileno(), {}, 3)
    assert not marker.exists()


def test_compaction_child_cannot_inherit_node_preload(native, tmp_path, monkeypatch):
    bridge, owner, epoch, witness = native
    marker = tmp_path / "untrusted-preload-executed"
    preload = tmp_path / "untrusted-loader.mjs"
    preload.write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(marker))}, 'executed');process.exit(29);"
    )
    monkeypatch.setenv("NODE_OPTIONS", f"--import={preload.as_uri()}")
    operation = bridge.commit(owner, epoch, witness, "isolated native mutation", 42)
    assert operation.status == "committed"
    assert not marker.exists()


async def test_active_backend_executor_refuses_before_intent_or_dispatch(native):
    bridge, owner, epoch, witness = native
    before = Path(witness["sessionFile"]).read_bytes()
    async with session_writer_fence(witness["sessionFile"]):
        with pytest.raises(SessionWriterBusyError, match="not dispatched"):
            bridge.commit(owner, epoch, witness, "summary", 42)
    assert bridge.journal.unresolved(witness["sessionFile"]) == ()
    assert Path(witness["sessionFile"]).read_bytes() == before


def test_json_source_is_not_accepted_as_owner_capture(native):
    bridge, owner, epoch, witness = native
    with pytest.raises(ValueError, match="Owner-captured"):
        OwnerCompactionCommit.commit(bridge, owner, epoch, witness, "summary", 42, source={})
    assert bridge.journal.unresolved(witness["sessionFile"]) == ()


def test_malformed_bus_refuses_source_capture_without_repair(native):
    bridge, owner, epoch, witness = native
    bus = bridge.root / "bus.jsonl"
    bus.write_bytes(b'{"incomplete":')
    with pytest.raises(RelationViolationError, match="Invalid compaction ingress"):
        bridge.capture_source(owner, epoch, witness)
    assert bus.read_bytes() == b'{"incomplete":'
    assert bridge.journal.unresolved(witness["sessionFile"]) == ()


def test_new_correction_send_invalidates_pre_summary_source(native):
    bridge, owner, epoch, witness = native
    comms = Comms(bridge.root)
    comms.register(Thread("peer", frozenset(), str(bridge.root)))
    comms.send("peer", "owner", "Correction: retain the newer requirement")
    with pytest.raises(RelationViolationError, match="source changed"):
        bridge.commit(owner, epoch, witness, "stale summary", 42)
    assert bridge.journal.unresolved(witness["sessionFile"]) == ()
    assert entries(witness)[-1]["type"] == "message"


def test_unsettled_input_refuses_preparation_and_commit_without_touching_unknown(native):
    bridge, owner, epoch, witness = native
    inputs = InputDispositions(bridge.root)
    inputs.record(
        "acp:queued",
        seq=None,
        owner=owner.name,
        admission=owner.active_turn.admission_generation,
        target=owner.name,
        text="queued correction",
    )
    with pytest.raises(RelationViolationError, match="Unsettled"):
        bridge.capture_source(owner, epoch, witness)
    with pytest.raises(RelationViolationError, match="Unsettled"):
        bridge.commit(owner, epoch, witness, "summary", 42)
    assert inputs.status("acp:queued") == "unknown"
    assert bridge.journal.unresolved(witness["sessionFile"]) == ()
    assert entries(witness)[-1]["type"] == "message"


def test_changed_started_input_still_invalidates_pre_summary_source(native):
    bridge, owner, epoch, witness = native
    inputs = InputDispositions(bridge.root)
    admission = owner.active_turn.admission_generation
    inputs.record(
        "acp:new",
        seq=None,
        owner=owner.name,
        admission=admission,
        target=owner.name,
        text="correction",
    )
    assert inputs.bind(
        "acp:new",
        admission=admission,
        turn_id=owner.active_turn.id,
        native_id="a" * 32,
        text="correction",
    )
    assert inputs.started(
        "acp:new", turn_id=owner.active_turn.id, native_id="a" * 32, text="correction"
    )
    with pytest.raises(RelationViolationError, match="source changed"):
        bridge.commit(owner, epoch, witness, "summary", 42)
    assert bridge.journal.unresolved(witness["sessionFile"]) == ()


def test_positive_owner_validated_native_commit(native):
    bridge, owner, epoch, witness = native
    operation = bridge.commit(owner, epoch, witness, "retained summary", 42)
    assert operation.status == "committed", operation
    entry = entries(witness)[-1]
    assert entry["type"] == "compaction"
    assert entry["summary"] == "retained summary"
    assert entry["details"]["agentCommsCommit"]["commitId"] == operation.commit_id
    assert json.loads(operation.evidence_json)["entryId"] == entry["id"]
    pending = bridge.journal.pending_publications(witness["sessionFile"])
    assert len(pending) == 1 and pending[0].commit_id == operation.commit_id
    assert json.loads(pending[0].metadata_json)["entryId"] == entry["id"]
    assert "retained summary" not in pending[0].metadata_json


@pytest.mark.parametrize("mutation", ["stop", "heartbeat", "goal", "bus", "input", "send"])
def test_competing_writer_waits_through_real_native_commit(native, monkeypatch, mutation):
    bridge, owner, epoch, witness = native
    call = bridge._call
    children = []
    script = """
import fcntl,sys
from pathlib import Path
from dataclasses import replace
from agent_comms.declarations import ThreadRegistry, Goal, Message, MessageBus, MessageType
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import Comms
root = Path(sys.argv[1])
mutation = sys.argv[2]
lock_name = {'bus':'bus.jsonl','input':'input_dispositions.json','send':'wire'}.get(
    mutation,'registry.json')
with (root / ('.' + lock_name + '.lock')).open('ab') as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('blocked',flush=True)
    else:
        raise AssertionError('authority escaped before native write')
if mutation == 'input':
    InputDispositions(root).record('acp:late',seq=None,owner='owner',
        admission=int(sys.argv[3]),target='owner',text='late correction')
else:
    registry = ThreadRegistry(root / 'registry.json')
    if mutation == 'stop':
        registry.unregister('owner')
    elif mutation == 'heartbeat':
        registry.heartbeat('owner')
    elif mutation == 'goal':
        owner = registry.snapshot().threads['owner']
        registry.register(replace(owner,goal=Goal('new','new-goal')))
    elif mutation == 'bus':
        MessageBus(root / 'bus.jsonl', registry).publish(
            Message(sender='owner',target='broadcast',body='late message',type=MessageType.INFO))
    else:
        Comms(root).send('owner','broadcast','late message')
print('changed',flush=True)
"""

    def with_competitor(*args):
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(bridge.journal.path.parent),
                mutation,
                str(owner.active_turn.admission_generation),
            ],
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
    assert [
        item.commit_id for item in bridge.journal.pending_publications(witness["sessionFile"])
    ] == [operation.commit_id]


def test_postcommit_directory_fsync_fault_is_unknown_and_never_dispatches(native, monkeypatch):
    bridge, owner, epoch, witness = native
    fsync = os.fsync
    called = []
    monkeypatch.setattr(bridge, "_call", lambda *args: called.append(args))

    def denied(fd):
        raise OSError("directory fsync denied")

    monkeypatch.setattr(os, "fsync", denied)
    with pytest.raises(CompactionJournalUnknownError, match="durability UNKNOWN"):
        bridge.commit(owner, epoch, witness, "summary", 42)
    assert called == []
    assert entries(witness)[-1]["type"] == "message"
    monkeypatch.setattr(os, "fsync", fsync)
    pending = bridge.journal.unresolved(witness["sessionFile"])
    assert len(pending) == 1 and pending[0].status == "intent"


def test_outcome_persistence_failure_keeps_intent_and_requires_reconciliation(native, monkeypatch):
    bridge, owner, epoch, witness = native
    resolve = bridge.journal.resolve

    def fail_outcome(*args, **kwargs):
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
witness = json.loads(sys.argv[3])
source = bridge.capture_source(owner,epoch,witness)
bridge.commit(owner,epoch,witness,'crash summary',42,source=source)
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


@pytest.mark.parametrize("release_native", [True, False], ids=["released", "hung-deadline"])
def test_parent_sigkill_after_stdin_before_native_write_retains_authority(
    native, tmp_path, release_native
):
    """A test-only JS wrapper gates the REAL native method, not a Python stand-in.

    The deployed helper/manager have no fault hooks. The wrapper installs an
    in-memory method barrier after inherited-FD/parent validation and request
    parsing, before CAS. Parent dies with stdin sent and no result read; native
    continues only when the test releases it, still owning registry authority.
    """
    bridge, owner, epoch, witness = native
    ready, gate, written = [tmp_path / name for name in ("ready.fifo", "go.fifo", "written.fifo")]
    for path in (ready, gate, written):
        os.mkfifo(path)
    handles = [os.open(path, os.O_RDWR | os.O_NONBLOCK) for path in (ready, gate, written)]
    # The production fence must NOT admit an external test wrapper. Publish a
    # separate test-only copied tree/pin containing the barrier, not a bypass.
    from agent_comms.native_package import TREE_PREFIX, package_tree_digest

    copied_package = tmp_path / "barrier-package"
    shutil.copytree(bridge.package_dir, copied_package)
    copied_helper = copied_package / "dist/agent-comms-compaction-commit-child.mjs"
    wrapper = copied_package / "dist/native-barrier.mjs"
    wrapper.write_text(
        "import {writeFileSync,openSync,readSync,closeSync} from 'node:fs';\n"
        "import {pathToFileURL} from 'node:url';\n"
        "const managerURL = "
        + json.dumps((copied_package / "dist/core/session-manager.js").as_uri())
        + ";\n"
        "const {SessionManager} = await import(managerURL);\n"
        "const append = SessionManager.prototype.appendCompactionIfCurrent;\n"
        "SessionManager.prototype.appendCompactionIfCurrent = function(...args) {\n"
        f"  writeFileSync({json.dumps(str(ready))}, String(process.pid));\n"
        f"  const gate = openSync({json.dumps(str(gate))}, 'r');\n"
        "  readSync(gate, Buffer.alloc(1), 0, 1, null); closeSync(gate);\n"
        "  const id = append.apply(this,args);\n"
        f"  writeFileSync({json.dumps(str(written))}, id);\n"
        "  return id;\n"
        "};\n"
        f"await import({json.dumps(copied_helper.as_uri())});\n"
    )
    test_manifest = tmp_path / "barrier-package.sha256"
    test_manifest.write_text(TREE_PREFIX + package_tree_digest(copied_package) + "\n")
    script = """
import json,os,sys
from pathlib import Path
from dataclasses import replace
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
import agent_comms.native_package as provenance
provenance.MANIFEST = Path(sys.argv[6])  # Test-only published tree including barrier.
bridge = OwnerCompactionCommit(Path(sys.argv[1]),Path(sys.argv[2]))
bridge.helper = Path(sys.argv[4])  # Test-only in-memory JS method barrier.
owner = bridge.registry.snapshot().threads['owner']
bridge.registry.unregister('owner')
bridge.registry.register(replace(owner,pid=os.getpid(),active_turn=None))
owner,epoch = bridge.registry.live_owner_with_epoch('owner')
owner,epoch = bridge.registry.claim_live_turn_with_epoch(owner,'crash',expected_epoch=epoch)
witness = json.loads(sys.argv[3])
source = bridge.capture_source(owner,epoch,witness)
bridge.commit(owner,epoch,witness,'post-parent-crash summary',42,source=source,
              timeout=float(sys.argv[5]))
"""
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "registry.json"),
            str(copied_package),
            json.dumps(witness),
            str(wrapper),
            "30" if release_native else "3",
            str(test_manifest),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    native_pid = None
    competitor = None

    def barrier(fd):
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            assert selector.select(10), "native barrier not reached"
        return os.read(fd, 1024)

    try:
        native_pid = int(barrier(handles[0]))
        pending = bridge.journal.unresolved(witness["sessionFile"])
        assert len(pending) == 1 and pending[0].status == "intent"
        before = Path(witness["sessionFile"]).read_bytes()
        parent.kill()
        assert parent.wait(timeout=5) == -signal.SIGKILL
        # Native is still blocked before mutation. A real registry stop cannot
        # pass the inherited authority held by that orphaned native process.
        competitor = subprocess.Popen(
            [
                sys.executable,
                "-c",
                """
import fcntl,sys
from pathlib import Path
from agent_comms.declarations import ThreadRegistry
root = Path(sys.argv[1])
with (root / '.registry.json.lock').open('ab') as lock:
    try:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise AssertionError('parent death released native authority')
for name in ('wire','bus.jsonl','input_dispositions.json'):
    with (root / ('.' + name + '.lock')).open('ab') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError('parent death released ingress exclusion: ' + name)
from agent_comms.session_fence import idle_session_writer_fence, SessionWriterBusyError
try:
    with idle_session_writer_fence(sys.argv[2]):
        raise AssertionError('parent death released native executor exclusion')
except SessionWriterBusyError:
    pass
print('still-fenced',flush=True)
ThreadRegistry(root / 'registry.json').unregister('owner')
print('stopped-after-native',flush=True)
""",
                str(tmp_path),
                witness["sessionFile"],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        with selectors.DefaultSelector() as selector:
            selector.register(competitor.stdout, selectors.EVENT_READ)
            assert selector.select(5)
        assert competitor.stdout.readline() == b"still-fenced\n"
        assert Path(witness["sessionFile"]).read_bytes() == before
        entry_id = None
        if release_native:
            os.write(handles[1], b"x")
            entry_id = barrier(handles[2]).decode()
        # Without release the native helper remains synchronously hung in a
        # real readSync. Only its independent surviving watchdog can kill it.
        output, error = competitor.communicate(timeout=5)
        assert competitor.returncode == 0, error
        assert output == b"stopped-after-native\n"
        native_pid = None  # Process has dropped its last authority FD at exit.
        if release_native:
            assert entries(witness)[-1]["id"] == entry_id
        else:
            assert Path(witness["sessionFile"]).read_bytes() == before
        assert bridge.journal.get(pending[0].commit_id).status == "intent"
        bridge.registry.register(replace(owner, active_turn=None))
        recovered, epoch = bridge.registry.live_owner_with_epoch("owner")
        recovered, epoch = bridge.registry.claim_live_turn_with_epoch(
            recovered,
            "recovery",
            expected_epoch=epoch,
        )
        result = bridge.reconcile(recovered, epoch, pending[0].commit_id)
        assert result.status == ("committed" if release_native else "aborted-no-write")
        if release_native:
            assert json.loads(result.evidence_json)["entryId"] == entry_id
        assert len([row for row in entries(witness) if row["type"] == "compaction"]) == int(
            release_native
        )
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait()
        if native_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(native_pid, signal.SIGKILL)
        if competitor is not None:
            if competitor.poll() is None:
                competitor.kill()
            competitor.wait()
            competitor.stdout.close()
            competitor.stderr.close()
        for fd in handles:
            os.close(fd)
        parent.stdout.close()
        parent.stderr.close()
