"""Opt-in real native CAS + canonical Python owner + durable journal integration.

Only PI_COMPACTION_TEST_PACKAGE selects a disposable, patched package. No
provider calls or installed package edits. Normal unit suites skip this file.
"""

import hashlib
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

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.compaction_errors import CompactionJournalError, CompactionJournalUnknownError
from agent_comms.compaction_states import UnknownNativeOutcome
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_compaction_writer import CompactionTransportUnknownError
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.owner_compaction_prepare import NativeWitness
from agent_comms.private_path import FileRevision
from agent_comms.pi_summary_payloads import SummaryFiles, SummaryUsage
from agent_comms.registration import Registration
from agent_comms.session_fence import SessionWriterBusyError, idle_session_writer_fence, session_writer_fence
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(
    not PACKAGE or sys.platform != "linux", reason="Disposable Linux native opt-in"
)


@pytest.fixture
def native(tmp_path):
    Comms(tmp_path).messaging.initialize_private_initial_protocol()
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
    witness = FieldCodec.decode(NativeWitness, json.loads(result.stdout))
    registry = Registration(tmp_path / "registry.json")
    owner = Thread(
        "owner",
        frozenset(),
        str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
        session_file=witness.session_file,
        goal=Goal("task", "goal"),
    )
    registry.register(owner)
    owner, owner_generation = registry.live_owner_with_generation("owner")
    owner, owner_generation = registry.lease_live_turn_with_generation(
        owner, "turn", expected_owner_generation=owner_generation
    )
    bridge = OwnerCompactionCommit(tmp_path / "registry.json", Path(PACKAGE))
    # Capture once BEFORE each test's summary/invalidations, never at commit.
    source = bridge.capture_source(owner, owner_generation, witness)
    bridge.commit = partial(bridge.commit, source=source)
    return bridge, owner, owner_generation, witness


def entries(witness):
    return [json.loads(line) for line in Path(witness.session_file).read_text().splitlines()]


async def test_acquired_native_source_survives_commit_and_refuses_rewritten_prefix(native):
    original_bridge, owner, generation, witness = native
    session = Path(witness.session_file)
    async with OwnerCompactionCommit.open(
        original_bridge.registry.store.path, Path(PACKAGE), witness.session_file
    ) as bridge:
        source = await Coordination.run_worker(partial(
            bridge.capture_source, owner, generation, witness
        ))
        await Coordination.run_worker(partial(
            bridge.require_source_current, owner, generation, source
        ))
        operation = await Coordination.run_worker(partial(
            bridge.commit, owner, generation, witness,
            source.retained.text + "\n\nOriginal task remains retained", 20,
            source=source,
        ))
        committed = source.after_native_commit(operation.committed_outcome())
        await Coordination.run_worker(partial(
            bridge.require_source_current, owner, generation, committed
        ))
        reader = bridge.boundary.native_reader
        # Fresh metadata cannot authorize changing the already certified bytes.
        raw = session.read_bytes()
        rewritten = raw.replace(b'"task"', b'"edit"', 1)
        assert rewritten != raw and len(rewritten) == len(raw)
        session.write_bytes(rewritten)
        fresh_stat = replace(committed, native=replace(
            committed.native, revision=FileRevision.from_stat(session.stat())
        ))
        with pytest.raises(NativePiUnavailable, match="original prefix changed"):
            await Coordination.run_worker(partial(
                bridge.require_source_current, owner, generation, fresh_stat
            ))
    assert reader.source.stream.closed
    assert bridge.journal.operations.get(operation.commit_id) == operation


def test_compaction_child_refuses_external_helper_before_execution(native, tmp_path):
    bridge, _, _, _ = native
    marker = tmp_path / "external-helper-executed"
    helper = tmp_path / "external-helper.mjs"
    helper.write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(marker))}, 'unsafe');"
    )
    bridge.native.helper = helper  # A trusted test's attempted override still cannot escape the fence.
    with (
        (tmp_path / "authority").open("w") as authority,
        pytest.raises(CompactionTransportUnknownError, match="Unparseable native outcome"),
    ):
        bridge.native.exchange(authority.fileno(), {}, 3)
    assert not marker.exists()


@pytest.mark.parametrize(
    "details",
    [
        {"readFiles": ["src/a.py"], "modifiedFiles": ["src/b.py"]},
        {
            "readFiles": [f"workspace/{'segment/' * 120}source{i:04}.py" for i in range(800)],
            "modifiedFiles": ["src/b.py"],
        },
    ],
    ids=["ordinary", "beyond-retired-count-metadata-and-request-caps"],
)
def test_native_file_operations_survive_journaled_commit(native, details):
    bridge, owner, owner_generation, witness = native
    source = bridge.capture_source(owner, owner_generation, witness)
    usage = {
        "input": 12,
        "output": 9,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 21,
        "cost": {"input": 0.01, "output": 0.02, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.03},
    }
    operation = OwnerCompactionCommit.commit(
        bridge,
        owner,
        owner_generation,
        witness,
        "Synthetic summary with file evidence",
        42,
        source=source,
        details=FieldCodec.decode(
            SummaryFiles, details
        ),
        usage=FieldCodec.decode(SummaryUsage, usage),
    )
    assert operation.state.declared_name == "committed"
    saved = entries(witness)[-1]
    assert saved["details"]["readFiles"] == details["readFiles"]
    assert saved["details"]["modifiedFiles"] == details["modifiedFiles"]
    assert saved["usage"] == usage
    assert saved["details"]["agentCommsCommit"]["commitId"] == operation.commit_id
    # Pi's next preparation consumes the prior structured details, not just
    # text that a later provider might omit. This executes native source only.
    script = """
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const root=process.argv[1], file=process.argv[2];
const {SessionManager}=await import(pathToFileURL(join(root,'dist/core/session-manager.js')));
const {prepareCompaction,DEFAULT_COMPACTION_SETTINGS}=await import(
  pathToFileURL(join(root,'dist/core/compaction/compaction.js')));
const {computeFileLists}=await import(pathToFileURL(join(root,'dist/core/compaction/utils.js')));
const manager=SessionManager.open(file);
manager.appendMessage({role:'user',content:'next task',timestamp:3});
manager.appendMessage({role:'assistant',content:[{type:'text',text:'next answer'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:4});
const prepared=prepareCompaction(manager.entryStore,
  {...DEFAULT_COMPACTION_SETTINGS,keepRecentTokens:1},{contextWindow:128000});
console.log(JSON.stringify(prepared && computeFileLists(prepared.fileOps)));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, PACKAGE, witness.session_file],
        capture_output=True,
        check=True,
        timeout=10,
        text=True,
    )
    assert json.loads(result.stdout) == details


def test_metadata_digest_preserves_unicode_paths_and_binary_costs(native):
    bridge, owner, owner_generation, witness = native
    source = bridge.capture_source(owner, owner_generation, witness)
    operation = OwnerCompactionCommit.commit(
        bridge,
        owner,
        owner_generation,
        witness,
        "summary",
        42,
        source=source,
        details=FieldCodec.decode(SummaryFiles, {"readFiles": ["src/⚙️-𝄞.py"], "modifiedFiles": []}),
        usage=FieldCodec.decode(
            SummaryUsage,
            {
                "input": 12,
                "output": 9,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 21,
                "reasoning": 0,
                "cost": {
                    "input": 0.0000001,
                    "output": 0.02,
                    "cacheRead": 0.0,
                    "cacheWrite": 0.0,
                    "total": 0.0200001,
                },
            },
        ),
    )
    assert operation.state.declared_name == "committed"
    row = entries(witness)[-1]
    assert row["details"]["readFiles"] == ["src/⚙️-𝄞.py"]
    assert (
        row["details"]["agentCommsCommit"]["metadataDigest"]
        == (json.loads(operation.intent_json)["metadataDigest"])
    )


@pytest.mark.parametrize("alter", ["details", "usage"])
def test_native_metadata_digest_refuses_changed_transport_before_write(native, alter):
    bridge, owner, owner_generation, witness = native
    source = bridge.capture_source(owner, owner_generation, witness)
    original = bridge.native.exchange
    before = Path(witness.session_file).read_bytes()
    usage = {
        "input": 12,
        "output": 9,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 21,
        "cost": {"input": 0.01, "output": 0.02, "cacheRead": 0, "cacheWrite": 0, "total": 0.03},
    }

    def changed_transport(fd, request, timeout, retained_fds=()):
        if alter == "details":
            altered = replace(request, details=replace(request.details, read_files=("src/other.py",)))
        else:
            altered = replace(request, usage=replace(request.usage, input=request.usage.input + 1))
        assert altered.commit.metadata_digest == request.commit.metadata_digest
        return original(fd, altered, timeout, retained_fds)

    bridge.native.exchange = changed_transport
    operation = OwnerCompactionCommit.commit(
        bridge,
        owner,
        owner_generation,
        witness,
        "summary",
        42,
        source=source,
        details=FieldCodec.decode(SummaryFiles, {"readFiles": ["src/a.py"], "modifiedFiles": []}),
        usage=FieldCodec.decode(SummaryUsage, usage),
    )
    assert operation.state.declared_name == "unknown"
    assert Path(witness.session_file).read_bytes() == before
    bridge.native.exchange = original
    assert (
        bridge.reconcile(owner, owner_generation, operation.commit_id).state.declared_name
        == "aborted-no-write"
    )


def test_metadata_transport_rehash_cannot_claim_wrong_native_commit(native):
    bridge, owner, owner_generation, witness = native
    source = bridge.capture_source(owner, owner_generation, witness)
    original = bridge.native.exchange

    def changed_transport(fd, request, timeout, retained_fds=()):
        altered = replace(request, details=replace(request.details, read_files=("src/other.py",)))
        canonical = [[[b"src/other.py".hex()], []], None]
        digest = hashlib.sha256(
            b"agent-comms-metadata-v1\n"
            + json.dumps(canonical, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        altered = replace(altered, commit=replace(altered.commit, metadata_digest=digest))
        return original(fd, altered, timeout, retained_fds)

    bridge.native.exchange = changed_transport
    operation = OwnerCompactionCommit.commit(
        bridge,
        owner,
        owner_generation,
        witness,
        "summary",
        42,
        source=source,
        details=FieldCodec.decode(SummaryFiles, {"readFiles": ["src/a.py"], "modifiedFiles": []}),
    )
    assert operation.state.declared_name == "unknown"
    assert entries(witness)[-1]["details"]["readFiles"] == ["src/other.py"]
    assert bridge.journal.publications.pending(witness.session_file) == ()
    bridge.native.exchange = original
    assert (
        bridge.reconcile(owner, owner_generation, operation.commit_id).state.declared_name
        == "unknown"
    )


@pytest.mark.parametrize("alter", ["details", "marker", "usage"])
def test_native_metadata_reconcile_refuses_changed_persisted_entry(native, alter):
    bridge, owner, owner_generation, witness = native
    source = bridge.capture_source(owner, owner_generation, witness)
    original = bridge.native.exchange

    def lost_result(fd, request, timeout, retained_fds=()):
        result = original(fd, request, timeout, retained_fds)
        assert result.state.committed
        return UnknownNativeOutcome("test-only lost receipt")

    bridge.native.exchange = lost_result
    operation = OwnerCompactionCommit.commit(
        bridge,
        owner,
        owner_generation,
        witness,
        "summary",
        42,
        source=source,
        details=FieldCodec.decode(SummaryFiles, {"readFiles": ["src/a.py"], "modifiedFiles": []}),
        usage=FieldCodec.decode(
            SummaryUsage,
            {
                "input": 12,
                "output": 9,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 21,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
        ),
    )
    assert operation.state.declared_name == "unknown"
    bridge.native.exchange = original
    rows = entries(witness)
    assert (
        rows[-1]["details"]["agentCommsCommit"]["metadataDigest"]
        == (json.loads(operation.intent_json)["metadataDigest"])
    )
    if alter == "details":
        rows[-1]["details"]["readFiles"] = ["src/other.py"]
    elif alter == "marker":
        rows[-1]["details"]["agentCommsCommit"]["metadataDigest"] = "0" * 64
    else:
        rows[-1]["usage"]["input"] += 1
    Path(witness.session_file).write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    )
    assert (
        bridge.reconcile(owner, owner_generation, operation.commit_id).state.declared_name
        == "unknown"
    )


def test_compaction_child_cannot_inherit_node_preload(native, tmp_path, monkeypatch):
    bridge, owner, owner_generation, witness = native
    marker = tmp_path / "untrusted-preload-executed"
    preload = tmp_path / "untrusted-loader.mjs"
    preload.write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(marker))}, 'executed');process.exit(29);"
    )
    monkeypatch.setenv("NODE_OPTIONS", f"--import={preload.as_uri()}")
    operation = bridge.commit(owner, owner_generation, witness, "isolated native mutation", 42)
    assert operation.state.declared_name == "committed"
    assert not marker.exists()


async def test_active_backend_executor_refuses_before_intent_or_dispatch(native):
    bridge, owner, owner_generation, witness = native
    before = Path(witness.session_file).read_bytes()
    async with session_writer_fence(witness.session_file):
        with pytest.raises(SessionWriterBusyError, match="not dispatched"):
            bridge.commit(owner, owner_generation, witness, "summary", 42)
    assert bridge.journal.operations.unresolved(witness.session_file) == ()
    assert Path(witness.session_file).read_bytes() == before


def test_malformed_bus_refuses_source_capture_without_repair(native):
    bridge, owner, owner_generation, witness = native
    bus = bridge.boundary.root / "bus.jsonl"
    Comms(bridge.boundary.root).messaging.initialize_private_initial_protocol()
    bus.write_bytes(b'{"incomplete":')
    with pytest.raises(RelationViolationError):
        bridge.capture_source(owner, owner_generation, witness)
    assert bus.read_bytes() == b'{"incomplete":'
    assert bridge.journal.operations.unresolved(witness.session_file) == ()


def test_new_correction_send_invalidates_pre_summary_source(native):
    bridge, owner, owner_generation, witness = native
    comms = Comms(bridge.boundary.root)
    comms.registry.declare(Thread("peer", frozenset(), str(bridge.boundary.root)))
    comms.messaging.send("peer", "owner", "Correction: retain the newer requirement")
    with pytest.raises(RelationViolationError, match="source changed"):
        bridge.commit(owner, owner_generation, witness, "stale summary", 42)
    assert bridge.journal.operations.unresolved(witness.session_file) == ()
    assert entries(witness)[-1]["type"] == "message"


def test_unsettled_input_refuses_preparation_and_commit_without_touching_unknown(native):
    bridge, owner, owner_generation, witness = native
    inputs = InputDispositions(bridge.boundary.root / InputDispositions.filename)
    inputs.record(
        "acp:queued",
        seq=None,
        owner=owner.name,
        admission=owner.active_turn.admission_generation,
        target=owner.name,
        text="queued correction",
    )
    assert inputs.bind(
        "acp:queued", admission=owner.active_turn.admission_generation,
        turn_id=owner.active_turn.id, native_id="a" * 32, text="queued correction",
    )
    uncertain = inputs.read().rows["acp:queued"]
    assert uncertain.declared_name == "bound_unknown"
    with pytest.raises(RelationViolationError, match="Unsettled"):
        bridge.capture_source(owner, owner_generation, witness)
    with pytest.raises(RelationViolationError, match="Unsettled"):
        bridge.commit(owner, owner_generation, witness, "summary", 42)
    assert inputs.read().rows["acp:queued"] == uncertain
    assert bridge.journal.operations.unresolved(witness.session_file) == ()
    assert entries(witness)[-1]["type"] == "message"


def test_only_exact_unattempted_original_input_can_cross_source_and_commit(native):
    bridge, owner, owner_generation, witness = native
    inputs = InputDispositions(bridge.boundary.root / InputDispositions.filename)
    admission = owner.active_turn.admission_generation
    assert admission is not None
    inputs.record(
        "acp:original",
        seq=None,
        owner=owner.name,
        admission=admission,
        target=owner.name,
        text="next input, not yet dispatched",
    )
    with pytest.raises(RelationViolationError, match="Unsettled"):
        bridge.capture_source(owner, owner_generation, witness)
    source = bridge.capture_source(
        owner, owner_generation, witness, pending_input_keys=("acp:original",)
    )
    assert source.pending_input_keys == ("acp:original",)
    result = OwnerCompactionCommit.commit(
        bridge, owner, owner_generation, witness, "summary", 42, source=source
    )
    assert result.state.declared_name == "committed"
    assert inputs.read().rows["acp:original"].declared_name == "reserved"
    assert not inputs.read().lookup("acp:original").has_native_binding


def test_original_input_exception_refuses_other_unknown_or_bound_original(native):
    bridge, owner, owner_generation, witness = native
    inputs = InputDispositions(bridge.boundary.root / InputDispositions.filename)
    admission = owner.active_turn.admission_generation
    assert admission is not None
    inputs.record(
        "acp:original",
        seq=None,
        owner=owner.name,
        admission=admission,
        target=owner.name,
        text="original",
    )
    source = bridge.capture_source(
        owner, owner_generation, witness, pending_input_keys=("acp:original",)
    )
    inputs.record(
        "acp:correction",
        seq=None,
        owner=owner.name,
        admission=admission,
        target=owner.name,
        text="correction",
    )
    with pytest.raises(RelationViolationError, match="Unsettled"):
        OwnerCompactionCommit.commit(
            bridge, owner, owner_generation, witness, "stale summary", 42, source=source
        )
    assert bridge.journal.operations.unresolved(witness.session_file) == ()
    assert inputs.bind(
        "acp:original",
        admission=admission,
        turn_id=owner.active_turn.id,
        native_id="a" * 32,
        text="original",
    )
    with pytest.raises(RelationViolationError, match="already attempted"):
        bridge.capture_source(owner, owner_generation, witness, pending_input_keys=("acp:original",))
    assert entries(witness)[-1]["type"] == "message"


def test_bus_unknown_cannot_borrow_direct_original_exception(native):
    bridge, owner, owner_generation, witness = native
    assert owner.active_turn is not None
    InputDispositions(bridge.boundary.root / InputDispositions.filename).record(
        "bus:1",
        seq=1,
        owner=owner.name,
        admission=owner.active_turn.admission_generation,
        target=owner.name,
        text="single bus original",
    )
    with pytest.raises(RelationViolationError, match="already attempted"):
        bridge.capture_source(owner, owner_generation, witness, pending_input_key="bus:1")
    assert entries(witness)[-1]["type"] == "message"


def test_changed_started_input_still_invalidates_pre_summary_source(native):
    bridge, owner, owner_generation, witness = native
    inputs = InputDispositions(bridge.boundary.root / InputDispositions.filename)
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
        bridge.commit(owner, owner_generation, witness, "summary", 42)
    assert bridge.journal.operations.unresolved(witness.session_file) == ()


def test_positive_owner_validated_native_commit(native):
    bridge, owner, owner_generation, witness = native
    operation = bridge.commit(owner, owner_generation, witness, "retained summary", 42)
    assert operation.state.declared_name == "committed", operation
    entry = entries(witness)[-1]
    assert entry["type"] == "compaction"
    assert entry["summary"] == "retained summary"
    assert entry["details"]["agentCommsCommit"]["commitId"] == operation.commit_id
    assert json.loads(operation.evidence_json)["entryId"] == entry["id"]
    pending = bridge.journal.publications.pending(witness.session_file)
    assert len(pending) == 1 and pending[0].commit_id == operation.commit_id
    assert json.loads(pending[0].metadata_json)["entryId"] == entry["id"]
    assert "retained summary" not in pending[0].metadata_json


def test_actual_compaction_child_retains_all_scoped_descriptors(native):
    """The real held boundary lends its descriptors through scope unwind."""
    bridge, owner, owner_generation, witness = native
    read_fd, write_fd = os.pipe()
    child = None
    try:
        with pytest.raises(RuntimeError, match="scope ended"):
            with bridge.boundary.hold(owner, owner_generation, witness) as held:
                descriptors = (held.authority_fd, *held.retained_fds)
                child = subprocess.Popen(
                    [sys.executable, "-c",
                     "import os,sys; [os.fstat(int(fd)) for fd in sys.argv[2:]]; "
                     "print('ready',flush=True); os.read(int(sys.argv[1]),1)",
                     str(read_fd), *(str(fd) for fd in descriptors)],
                    pass_fds=(*descriptors, read_fd),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                )
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    assert selector.select(5), "actual inherited child did not start"
                assert child.stdout.readline() == b"ready\n"
                raise RuntimeError("scope ended")
        for path in (
            bridge.boundary.root / "wire", bridge.boundary.root / "bus.jsonl",
            bridge.registry.store.path, bridge.inputs.path,
        ):
            with pytest.raises(BlockingIOError), _store_lock(path, blocking=False):
                pass
        with pytest.raises(SessionWriterBusyError), idle_session_writer_fence(witness.session_file):
            pass
        os.write(write_fd, b"x")
        output, error = child.communicate(timeout=5)
        assert child.returncode == 0, error
        assert output == b""
        for path in (
            bridge.boundary.root / "wire", bridge.boundary.root / "bus.jsonl",
            bridge.registry.store.path, bridge.inputs.path,
        ):
            with _store_lock(path, blocking=False):
                pass
        with idle_session_writer_fence(witness.session_file):
            pass
    finally:
        os.close(read_fd)
        os.close(write_fd)
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()
            child.stderr.close()


@pytest.mark.parametrize("mutation", ["stop", "heartbeat", "goal", "bus", "input", "send"])
def test_competing_writer_waits_through_real_native_commit(native, monkeypatch, mutation):
    bridge, owner, owner_generation, witness = native
    call = bridge.native.exchange
    children = []
    script = """
import fcntl, sys
from pathlib import Path
from dataclasses import replace
from agent_comms.child_process import ProcessIdentity
from agent_comms.goals import Goal
from agent_comms.registration import Registration
from agent_comms.input_disposition import InputDispositions
from agent_comms.comms import Comms
root = Path(sys.argv[1])
mutation = sys.argv[2]
lock_name = {'bus': 'bus.jsonl', 'input': 'input_dispositions.json', 'send': 'wire'}.get(
    mutation, 'registry.json')
with (root / ('.' + lock_name + '.lock')).open('ab') as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('blocked', flush=True)
    else:
        raise AssertionError('authority escaped before native write')
if mutation == 'input':
    InputDispositions(root / InputDispositions.filename).record(
        'acp:late', seq=None, owner='owner', admission=int(sys.argv[3]),
        target='owner', text='late correction')
else:
    registry = Registration(root / 'registry.json')
    if mutation == 'stop':
        registry.unregister('owner')
    elif mutation == 'heartbeat':
        registry.heartbeat('owner')
    elif mutation == 'goal':
        owner = registry.snapshot().threads['owner']
        registry.register(replace(owner, goal=Goal('new', 'new-goal')))
    else:
        Comms(root).messaging.send('owner', '#all', 'late message')
print('changed', flush=True)
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

    monkeypatch.setattr(bridge.native, "exchange", with_competitor)
    try:
        operation = bridge.commit(owner, owner_generation, witness, "summary", 42)
        assert operation.state.declared_name == "committed"
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
    bridge, owner, owner_generation, witness = native
    call = bridge.native.exchange

    def lose_result(*args):
        result = call(*args)
        assert result.state.committed, result
        raise CompactionTransportUnknownError("lost result")

    monkeypatch.setattr(bridge.native, "exchange", lose_result)
    operation = bridge.commit(owner, owner_generation, witness, "retained summary", 42)
    assert operation.state.declared_name == "unknown"
    before = Path(witness.session_file).read_bytes()
    with pytest.raises(CompactionJournalError, match="never replay"):
        bridge.commit(owner, owner_generation, witness, "retained summary", 42)
    monkeypatch.setattr(bridge.native, "exchange", call)
    resolved = bridge.reconcile(owner, owner_generation, operation.commit_id)
    assert resolved.state.declared_name == "committed"
    assert Path(witness.session_file).read_bytes() == before
    assert len([entry for entry in entries(witness) if entry["type"] == "compaction"]) == 1
    assert [
        item.commit_id for item in bridge.journal.publications.pending(witness.session_file)
    ] == [operation.commit_id]


def test_postcommit_directory_fsync_fault_is_unknown_and_never_dispatches(native, monkeypatch):
    bridge, owner, owner_generation, witness = native
    fsync = os.fsync
    called = []
    monkeypatch.setattr(bridge.native, "exchange", lambda *args: called.append(args))

    def denied(fd):
        raise OSError("directory fsync denied")

    monkeypatch.setattr(os, "fsync", denied)
    with pytest.raises(CompactionJournalUnknownError, match="durability UNKNOWN"):
        bridge.commit(owner, owner_generation, witness, "summary", 42)
    assert called == []
    assert entries(witness)[-1]["type"] == "message"
    monkeypatch.setattr(os, "fsync", fsync)
    pending = bridge.journal.operations.unresolved(witness.session_file)
    assert len(pending) == 1 and pending[0].state.declared_name == "intent"


def test_outcome_persistence_failure_keeps_intent_and_requires_reconciliation(native, monkeypatch):
    bridge, owner, owner_generation, witness = native
    resolve = bridge.journal.operations.resolve

    def fail_outcome(*args, **kwargs):
        raise OSError("outcome fsync unavailable")

    monkeypatch.setattr(bridge.journal.operations, "resolve", fail_outcome)
    with pytest.raises(OSError, match="outcome fsync"):
        bridge.commit(owner, owner_generation, witness, "summary", 42)
    pending = bridge.journal.operations.unresolved(witness.session_file)
    assert len(pending) == 1 and pending[0].state.declared_name == "intent"
    with pytest.raises(CompactionJournalError, match="never replay"):
        bridge.commit(owner, owner_generation, witness, "summary", 42)
    monkeypatch.setattr(bridge.journal.operations, "resolve", resolve)
    assert (
        bridge.reconcile(owner, owner_generation, pending[0].commit_id).state.declared_name
        == "committed"
    )


def test_missing_write_reconciles_absence_only_at_unchanged_revision(native, monkeypatch):
    bridge, owner, owner_generation, witness = native
    call = bridge.native.exchange

    def never_started(*args):
        raise CompactionTransportUnknownError("transport unavailable")

    monkeypatch.setattr(bridge.native, "exchange", never_started)
    operation = bridge.commit(owner, owner_generation, witness, "summary", 42)
    assert operation.state.declared_name == "unknown"
    monkeypatch.setattr(bridge.native, "exchange", call)
    assert (
        bridge.reconcile(owner, owner_generation, operation.commit_id).state.declared_name
        == "aborted-no-write"
    )
    with pytest.raises(CompactionJournalError):
        bridge.journal.operations.begin(
            witness.session_file,
            {},
            commit_id=operation.commit_id,
            inputs=InputDispositions(
                bridge.journal.path.parent / InputDispositions.filename
            ).read(),
        )


def test_stopped_owner_refused_before_journal_or_native_mutation(native):
    bridge, owner, owner_generation, witness = native
    before = Path(witness.session_file).read_bytes()
    bridge.registry.unregister(owner.name)
    with pytest.raises(RelationViolationError):
        bridge.commit(owner, owner_generation, witness, "summary", 42)
    assert Path(witness.session_file).read_bytes() == before


def test_changed_goal_refused_before_native_mutation(native):
    bridge, owner, owner_generation, witness = native
    bridge.registry.register(replace(owner, goal=Goal("replacement", "new-goal")))
    with pytest.raises(RelationViolationError):
        bridge.commit(owner, owner_generation, witness, "summary", 42)
    assert entries(witness)[-1]["type"] == "message"


def test_forged_receipt_without_inherited_fd_never_writes(native):
    bridge, owner, owner_generation, witness = native
    before = Path(witness.session_file).read_bytes()
    result = subprocess.run(
        ["node", str(bridge.native.helper), str(bridge.native.package_dir), "-1"],
        input=json.dumps(
            {"attestation": {"thread": owner.name}, "witness": FieldCodec.encode(witness)}
        ).encode(),
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["status"] == "unknown"
    assert Path(witness.session_file).read_bytes() == before


def test_native_stale_lock_requires_explicit_reconciliation(native):
    bridge, owner, owner_generation, witness = native
    lock = Path(witness.session_file + ".pr48-writer.lock")
    lock.write_text("fixture dead holder\n")
    operation = bridge.commit(owner, owner_generation, witness, "summary", 42)
    assert operation.state.declared_name == "unknown"
    assert (
        bridge.reconcile(owner, owner_generation, operation.commit_id).state.declared_name
        == "unknown"
    )
    lock.unlink()  # Explicit fixture operator decision; never automatic recovery.
    assert bridge.journal.operations.get(operation.commit_id).state.declared_name == "unknown"
    assert (
        bridge.reconcile(owner, owner_generation, operation.commit_id).state.declared_name
        == "aborted-no-write"
    )


def test_owner_sigkill_after_native_write_before_journal_result(native, tmp_path):
    bridge, owner, owner_generation, witness = native
    script = """
import json,os,signal,sys
from pathlib import Path
from dataclasses import replace
from agent_comms.child_process import ProcessIdentity
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.child_process import ProcessIdentity
bridge = OwnerCompactionCommit(Path(sys.argv[1]), Path(sys.argv[2]))
owner = bridge.registry.snapshot().threads['owner']
bridge.registry.unregister('owner')
bridge.registry.register(replace(owner,process_identity=ProcessIdentity.capture(os.getpid()),active_turn=None))
owner,epoch = bridge.registry.live_owner_with_generation('owner')
owner,epoch = bridge.registry.lease_live_turn_with_generation(
    owner,'crash-turn',expected_owner_generation=epoch)
call = bridge.native.exchange
def lose_result(*args):
    result = call(*args)
    assert result.state.committed, result
    print('native-durable-before-journal-result',flush=True)
    signal.pause()
bridge.native.exchange = lose_result
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_prepare import NativeWitness
witness = FieldCodec.decode(NativeWitness, json.loads(sys.argv[3]))
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
            json.dumps(FieldCodec.encode(witness)),
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
        pending = bridge.journal.operations.unresolved(witness.session_file)
        assert len(pending) == 1
        assert pending[0].state.declared_name == "intent"
        before = Path(witness.session_file).read_bytes()
        # Explicit recovery under a fresh canonical owner/turn, no replay of
        # the stopped owner's request and no reuse of its epoch or receipt.
        bridge.registry.unregister("owner")
        bridge.registry.register(replace(owner, active_turn=None))
        recovered, owner_generation = bridge.registry.live_owner_with_generation("owner")
        recovered, owner_generation = bridge.registry.lease_live_turn_with_generation(
            recovered,
            "recovery-turn",
            expected_owner_generation=owner_generation,
        )
        result = bridge.reconcile(recovered, owner_generation, pending[0].commit_id)
        assert result.state.declared_name == "committed"
        assert Path(witness.session_file).read_bytes() == before
        assert bridge.journal.operations.unresolved(witness.session_file) == ()
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
    bridge, owner, owner_generation, witness = native
    ready, gate, written = [tmp_path / name for name in ("ready.fifo", "go.fifo", "written.fifo")]
    for path in (ready, gate, written):
        os.mkfifo(path)
    handles = [os.open(path, os.O_RDWR | os.O_NONBLOCK) for path in (ready, gate, written)]
    # The production fence must NOT admit an external test wrapper. Publish a
    # separate test-only copied tree/pin containing the barrier, not a bypass.
    from agent_comms.native_package import TREE_PREFIX, package_tree_digest

    copied_package = tmp_path / "barrier-package"
    shutil.copytree(bridge.native.package_dir, copied_package)
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
from agent_comms.child_process import ProcessIdentity
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.child_process import ProcessIdentity
import agent_comms.native_package as provenance
provenance.MANIFEST = Path(sys.argv[6])  # Test-only published tree including barrier.
bridge = OwnerCompactionCommit(Path(sys.argv[1]),Path(sys.argv[2]))
bridge.native.helper = Path(sys.argv[4])  # Test-only in-memory JS method barrier.
owner = bridge.registry.snapshot().threads['owner']
bridge.registry.unregister('owner')
bridge.registry.register(replace(owner,process_identity=ProcessIdentity.capture(os.getpid()),active_turn=None))
owner,epoch = bridge.registry.live_owner_with_generation('owner')
owner,epoch = bridge.registry.lease_live_turn_with_generation(
    owner,'crash',expected_owner_generation=epoch)
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_prepare import NativeWitness
witness = FieldCodec.decode(NativeWitness, json.loads(sys.argv[3]))
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
            json.dumps(FieldCodec.encode(witness)),
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
        pending = bridge.journal.operations.unresolved(witness.session_file)
        assert len(pending) == 1 and pending[0].state.declared_name == "intent"
        before = Path(witness.session_file).read_bytes()
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
from agent_comms.registration import Registration
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
Registration(root / 'registry.json').unregister('owner')
print('stopped-after-native',flush=True)
""",
                str(tmp_path),
                witness.session_file,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        with selectors.DefaultSelector() as selector:
            selector.register(competitor.stdout, selectors.EVENT_READ)
            assert selector.select(5)
        assert competitor.stdout.readline() == b"still-fenced\n"
        assert Path(witness.session_file).read_bytes() == before
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
            assert Path(witness.session_file).read_bytes() == before
        assert bridge.journal.operations.get(pending[0].commit_id).state.declared_name == "intent"
        bridge.registry.register(replace(owner, active_turn=None))
        recovered, owner_generation = bridge.registry.live_owner_with_generation("owner")
        recovered, owner_generation = bridge.registry.lease_live_turn_with_generation(
            recovered,
            "recovery",
            expected_owner_generation=owner_generation,
        )
        result = bridge.reconcile(recovered, owner_generation, pending[0].commit_id)
        assert result.state.declared_name == ("committed" if release_native else "aborted-no-write")
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
