from agent_comms.acp_extension import CompactionPublishedUpdate, decode_updates

"""Provider-free read-only preparation from the exact disposable Pi tree."""

import asyncio
import hashlib
import json
import os
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import backend, owner_compaction_runtime
from agent_comms.backend import PersistentPiSession
from agent_comms.child_process import AttachedChild, Platform, ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.compaction_publication import publish_pending_local
from agent_comms.errors import RelationViolationError
from agent_comms.goals import Goal
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from agent_comms.owner_compaction_prepare import NativePreparationError, prepare_native_source
from agent_comms.owner_compaction_provider import NativeSummary
from agent_comms.owner_compaction_runtime import compact_owner_once
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.registration import Registration
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(
    not PACKAGE or sys.platform != "linux", reason="Disposable Linux native opt-in"
)


@pytest.fixture
def session(tmp_path):
    script = """
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const {SessionManager} = await import(pathToFileURL(join(process.argv[1],
  'dist/core/session-manager.js')));
const manager = SessionManager.create(process.argv[2], join(process.argv[2], 'sessions'));
manager.appendMessage({role:'user', content:'First task '+('history '.repeat(500)), timestamp:1});
manager.appendMessage({role:'assistant', content:[{type:'text',text:'First answer'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:2});
manager.appendMessage({role:'user', content:'Next task', timestamp:3});
manager.appendMessage({role:'assistant', content:[{type:'text',text:'Next answer'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:4});
console.log(manager.getSessionFile());
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, PACKAGE, str(tmp_path)],
        capture_output=True,
        check=True,
        timeout=5,
        text=True,
    )
    return Path(result.stdout.strip())


def test_native_preparation_is_read_only_and_matches_saved_cutpoint(session):
    original = session.read_bytes()
    package = Path(PACKAGE)
    prepared = prepare_native_source(
        package, str(session), settings=PiCompactionSettings(16384, 1), context_window=128000
    )
    assert prepared is not None
    assert prepared.witness.session_id
    assert prepared.witness.session_file == str(session)
    assert prepared.tokens_before > 0
    assert any(
        json.loads(row).get("id") == prepared.witness.first_kept_entry_id
        for row in original.splitlines()
    )
    assert session.read_bytes() == original
    assert (
        prepare_native_source(
            package,
            str(session),
            settings=PiCompactionSettings(16384, 20000),
            context_window=128000,
        )
        is None
    )
    assert session.read_bytes() == original


def test_canonical_owner_prepares_source_before_summary_and_commits_once(session):
    root = session.parent.parent
    registry = Registration(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(session),
            goal=Goal("task", "goal"),
        )
    )
    owner, owner_generation = registry.live_owner_with_generation("owner")
    owner, owner_generation = registry.lease_live_turn_with_generation(
        owner, "turn", expected_owner_generation=owner_generation
    )
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    before = session.read_bytes()
    candidate = bridge.prepare_source(
        owner, owner_generation, settings=PiCompactionSettings(16384, 1), context_window=128000
    )
    assert candidate is not None
    prepared, source = candidate
    assert session.read_bytes() == before
    operation = bridge.commit(
        owner,
        owner_generation,
        prepared.witness,
        "Provider-free synthetic summary",
        prepared.tokens_before,
        source=source,
    )
    assert operation.state.declared_name == "committed"
    assert bridge.journal.unresolved(str(session)) == ()
    assert len(bridge.journal.pending_publications(str(session))) == 1


def write_capacity_history(file: Path, minimum_bytes: int) -> dict:
    """Real v3 disk history; generation retains at most one modest record."""
    timestamp = "2026-09-28T00:00:00.000Z"
    usage = {
        "input": 2000,
        "output": 1,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 2001,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }
    with file.open("xb") as stream:

        def row(value):
            stream.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")

        def entry(kind, identity, parent, **values):
            row(dict(type=kind, id=identity, parentId=parent, timestamp=timestamp, **values))

        def user(identity, parent, text):
            entry(
                "message",
                identity,
                parent,
                message={
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": 1,
                },
            )

        def assistant(identity, parent):
            entry(
                "message",
                identity,
                parent,
                message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ack"}],
                    "timestamp": 2,
                    "provider": "fixture",
                    "model": "fixture",
                    "api": "openai-completions",
                    "stopReason": "stop",
                    "usage": usage,
                },
            )

        row(
            dict(
                type="session",
                version=3,
                id="capacity-session",
                timestamp=timestamp,
                cwd=str(file.parent),
            )
        )
        entry("model_change", "model-before-floor", None, provider="fixture", modelId="fixture")
        entry(
            "thinking_level_change",
            "thinking-before-floor",
            "model-before-floor",
            thinkingLevel="low",
        )
        user("seed-user", "thinking-before-floor", "old task")
        assistant("seed-answer", "seed-user")
        old_summary = "historical commit outside the retained context"
        old_commit = {
            "commitId": "a" * 32,
            "payloadDigest": hashlib.sha256(
                json.dumps([old_summary, "seed-user", 2001], separators=(",", ":")).encode()
            ).hexdigest(),
            "metadataDigest": hashlib.sha256(b"agent-comms-metadata-v1\n[null,null]").hexdigest(),
        }
        entry(
            "compaction",
            "old-commit",
            "seed-answer",
            summary=old_summary,
            firstKeptEntryId="seed-user",
            tokensBefore=2001,
            details={"agentCommsCommit": old_commit},
        )
        # A different branch has a distinct summary and settings. It must not
        # inherit the physically later compaction from the selected main branch.
        user("side-user", "seed-answer", "SIDE_BRANCH_ONLY")
        assistant("side-answer", "side-user")
        entry(
            "compaction",
            "side-compaction",
            "side-answer",
            summary="SIDE_SUMMARY_ONLY",
            firstKeptEntryId="side-user",
            tokensBefore=2001,
        )
        parent = "old-commit"
        count = 0
        payload = "OBSOLETE_LARGE_PAYLOAD " + "x" * 65536
        while stream.tell() < minimum_bytes:
            user_id = f"history-user-{count}"
            user(user_id, parent, payload)
            parent = f"history-answer-{count}"
            assistant(parent, user_id)
            count += 1
        user("retained-user", parent, "retained recent question")
        assistant("retained-answer", "retained-user")
        entry(
            "compaction",
            "retained-floor",
            "retained-answer",
            summary="MAIN_RETAINED_SUMMARY",
            firstKeptEntryId="retained-user",
            tokensBefore=2001,
        )
        parent = "retained-floor"
        for index in range(4):
            user_id = f"active-user-{index}"
            user(user_id, parent, "ACTIVE_BRANCH_MARKER " + "current " * 200)
            parent = f"active-answer-{index}"
            assistant(parent, user_id)
    file.chmod(0o600)
    return {
        "session_id": "capacity-session",
        "old_commit": old_commit,
        "old_summary": old_summary,
        "historical_pairs": count,
    }


@contextmanager
def native_memory_budget(root: Path, monkeypatch, *, rss_mib=240, heap_mib=128):
    """Test-only observer: real native programs, exact-child RSS enforcement."""
    node = shutil.which("node")
    assert node
    starts = root / "native-starts.jsonl"
    exits = root / "native-exits.jsonl"
    observer = root / "observe-native.mjs"
    observer.write_text(
        "import {appendFileSync,readFileSync} from 'node:fs';"
        "import http from 'node:http';import https from 'node:https';import net from 'node:net';"
        "import {syncBuiltinESMExports} from 'node:module';"
        "const deny=()=>{throw Error('NETWORK_PROHIBITED_IN_CAPACITY_ACCEPTANCE')};"
        "http.request=http.get=https.request=https.get=net.connect=net.createConnection=deny;"
        "globalThis.fetch=deny;syncBuiltinESMExports();"
        "const stat=readFileSync('/proc/self/stat','utf8');"
        "const start_time=Number(stat.slice(stat.lastIndexOf(')')+2).split(' ')[19]);"
        f"appendFileSync({json.dumps(str(starts))},JSON.stringify({{pid:process.pid,"
        "start_time,phase:process.env.AC_CAPACITY_PHASE})+'\\n');"
        "process.on('exit',()=>{"
        f"appendFileSync({json.dumps(str(exits))},JSON.stringify({{pid:process.pid,"
        "phase:process.env.AC_CAPACITY_PHASE,peak_rss_kib:"
        "Number(readFileSync('/proc/self/status','utf8').match(/^VmHWM:\\s+(\\d+)/m)[1]),"
        "rusage_peak_rss_kib:process.resourceUsage().maxRSS,"
        "memory:process.memoryUsage()})+'\\n')});"
    )
    binary = root / "bin"
    binary.mkdir()
    wrapper = binary / "node"
    wrapper.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(node)
        + f" --max-old-space-size={heap_mib} --import="
        + shlex.quote(str(observer))
        + ' "$@"\n'
    )
    wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    # A deliberately constrained V8 must never leave a large core dump.
    previous_core = resource.getrlimit(resource.RLIMIT_CORE)
    resource.setrlimit(resource.RLIMIT_CORE, (0, previous_core[1]))
    stopped = threading.Event()
    peaks = {}
    identities = {}
    exceeded = []
    monitor_errors = []
    parent_identity = ProcessIdentity.capture(os.getpid())

    def parent_peak():
        # Linux rusage can retain a high water from before exec. VmHWM
        # measures this test executable, not its tool-launcher's old image.
        return next(
            int(line.split()[1])
            for line in Path("/proc/self/status").read_text().splitlines()
            if line.startswith("VmHWM:")
        )

    def sample():
        peak = parent_peak()
        if peak > rss_mib * 1024 and not exceeded:
            exceeded.append({"phase": "python-owner", "peak_rss_kib": peak})
            # Interrupt the owned test runner so its finally blocks retire
            # children and remove generated data instead of allowing an OOM.
            Platform.current().send(parent_identity, signal.SIGINT)
        if not starts.exists():
            return
        for raw in starts.read_bytes().splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                continue
            observed = json.loads(raw)
            pid = observed["pid"]
            try:
                if pid not in identities:
                    identities[pid] = ProcessIdentity(pid, observed["start_time"])
                identity = identities[pid]
                if not identity.alive():
                    continue
                status = Path(f"/proc/{pid}/status").read_text()
                peak = next(
                    (
                        int(line.split()[1])
                        for line in status.splitlines()
                        if line.startswith("VmHWM:")
                    ),
                    0,
                )
                peaks[pid] = max(peaks.get(pid, 0), peak)
                if peak > rss_mib * 1024:
                    exceeded.append(dict(observed, peak_rss_kib=peak))
                    Platform.current().send(identity, signal.SIGKILL)
            except (ProcessLookupError, FileNotFoundError):
                continue

    def monitor():
        try:
            while not stopped.wait(0.01):
                sample()
        except Exception as error:
            monitor_errors.append(repr(error))

    thread = threading.Thread(target=monitor, name="native-capacity-rss", daemon=True)
    thread.start()
    receipt = {"rss_limit_mib": rss_mib, "v8_old_space_mib": heap_mib}
    try:
        yield receipt
    finally:
        stopped.set()
        thread.join(timeout=2)
        sample()
        resource.setrlimit(resource.RLIMIT_CORE, previous_core)
        observations = (
            [json.loads(line) for line in exits.read_bytes().splitlines()] if exits.exists() else []
        )
        receipt.update(
            processes=observations,
            sampled_peak_rss_kib=peaks,
            python_peak_rss_kib=parent_peak(),
            exceeded=exceeded,
            monitor_errors=monitor_errors,
        )
        print("CAPACITY_MEMORY " + json.dumps(receipt), flush=True)
        assert not thread.is_alive() and not monitor_errors, receipt
        assert not exceeded, receipt
        assert receipt["python_peak_rss_kib"] <= rss_mib * 1024, receipt
        assert all(item["peak_rss_kib"] <= rss_mib * 1024 for item in observations), receipt
        assert all(not identity.alive() for identity in identities.values()), receipt


async def capacity_native_cli(
    package: Path, session: Path, root: Path, *, expected_session_id: str
):
    """Actual CLI startup/reopen and correlated state; never send a prompt."""
    child = await AttachedChild.start(
        (
            "node",
            str(package / "dist/cli.js"),
            "--mode",
            "rpc",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-tools",
            "--session",
            str(session),
        ),
        cwd=root,
    )
    stderr = asyncio.create_task(child.stderr.read())
    try:
        async with asyncio.timeout(25):
            for command in ("get_state", "get_messages"):
                child.stdin.write(json.dumps({"type": command, "id": command}).encode() + b"\n")
                await child.stdin.drain()
                while True:
                    raw = await child.stdout.readline()
                    assert raw, (await stderr).decode(errors="replace")
                    event = json.loads(raw)
                    assert event.get("type") not in {"input_committed", "context_committed"}, event
                    if event.get("type") == "response" and event.get("id") == command:
                        assert event["success"], event
                        if command == "get_state":
                            assert event["data"]["sessionId"] == expected_session_id, event
                            assert Path(event["data"]["sessionFile"]) == session, event
                            assert (
                                event["data"]["nativeInputProofCapability"]
                                == "pi-native-input-v1-live-only"
                            ), event
                        else:
                            assert b"MAIN_RETAINED_SUMMARY" in raw or b"Capacity accepted" in raw
                            assert b"OBSOLETE_LARGE_PAYLOAD" not in raw
                            assert b"SIDE_BRANCH_ONLY" not in raw
                        break
    finally:
        await child.finish()
        errors = await stderr
        if errors:
            print(errors.decode(errors="replace")[-4000:])
        assert not child.identity.alive()


@pytest.mark.skipif(
    os.environ.get("AC_NATIVE_LARGE_HISTORY") != "1",
    reason="Explicit >256MiB disk/native memory acceptance",
)
@pytest.mark.parametrize("history_mib", [288, 576])
def test_large_history_cli_prepare_commit_reopen_under_memory_budget(
    tmp_path, monkeypatch, history_mib
):
    """No external calls or retained user data; exercise the production chain."""
    package = Path(PACKAGE).resolve()
    assert "PYTEST_XDIST_WORKER" not in os.environ, "Run capacity acceptance serially with -n0"
    minimum = history_mib * 1024**2
    assert shutil.disk_usage(tmp_path).free > minimum * 3, "Insufficient owned disk fixture space"
    root = tmp_path / "capacity"
    root.mkdir(mode=0o700)
    session = root / "capacity.jsonl"
    receipt = {"phases": [], "provider_calls": 0}
    try:
        fixture = write_capacity_history(session, minimum)
        fixture_path = root / "capacity-fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        receipt.update(fixture, source_bytes=session.stat().st_size)
        with session.open("rb") as stream:
            before = hashlib.file_digest(stream, "sha256").hexdigest()
        config = root / "config"
        config.mkdir()
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
        monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
        monkeypatch.setenv("AGENT_COMMS_SESSION_INDEX_DIR", str(root / "indexes"))
        monkeypatch.setenv("PI_OFFLINE", "1")
        with native_memory_budget(root, monkeypatch) as memory:
            receipt["memory"] = memory
            monkeypatch.setenv("AC_CAPACITY_PHASE", "cli-open")
            asyncio.run(
                capacity_native_cli(
                    package, session, root, expected_session_id=fixture["session_id"]
                )
            )
            receipt["phases"].append("cli-open")
            monkeypatch.setenv("AC_CAPACITY_PHASE", "branch-replay-malformed")
            monkeypatch.setenv("AC_CAPACITY_SESSION", str(session))
            monkeypatch.setenv("AC_CAPACITY_FIXTURE", str(fixture_path))
            monkeypatch.setenv("PI_NATIVE_PACKAGE_DIR", str(package))
            monkeypatch.setenv("TMPDIR", str(root))
            probe = Path(__file__).resolve().parents[1] / "stack/test-native-writer-coverage.mjs"
            checked = subprocess.run(
                ["node", str(probe), "large-history-branch-replay"],
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert checked.returncode == 0, checked.stdout + checked.stderr
            print(checked.stdout)
            receipt["phases"].append("branch-replay-malformed")
            registry = Registration(root / "registry.json")
            registry.register(
                Thread(
                    "owner",
                    frozenset(),
                    str(root),
                    process_identity=ProcessIdentity.capture(os.getpid()),
                    session_file=str(session),
                    goal=Goal("capacity acceptance", "goal"),
                )
            )
            owner, generation = registry.live_owner_with_generation("owner")
            owner, generation = registry.lease_live_turn_with_generation(
                owner,
                "turn",
                expected_owner_generation=generation,
            )
            bridge = OwnerCompactionCommit(root / "registry.json", package)
            monkeypatch.setenv("AC_CAPACITY_PHASE", "prepare")
            candidate = bridge.prepare_source(
                owner, generation, settings=PiCompactionSettings(16384, 1), context_window=128000
            )
            assert candidate is not None
            prepared, source = candidate
            receipt["phases"].append("prepare")
            assert prepared.witness.session_id == fixture["session_id"]
            with session.open("rb") as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == before
            monkeypatch.setenv("AC_CAPACITY_PHASE", "commit")
            operation = bridge.commit(
                owner,
                generation,
                prepared.witness,
                "Capacity accepted",
                prepared.tokens_before,
                source=source,
                timeout=30,
            )
            assert operation.state.committed
            assert not bridge.journal.unresolved(str(session))
            receipt["phases"].append("commit")
            from agent_comms.native_session_reopen import validate_native_reopen

            monkeypatch.setenv("AC_CAPACITY_PHASE", "strict-reopen")
            assert (
                validate_native_reopen(
                    package, str(session), expected_session_id=fixture["session_id"]
                )
                == fixture["session_id"]
            )
            receipt["phases"].append("strict-reopen")
            monkeypatch.setenv("AC_CAPACITY_PHASE", "cli-reopen")
            asyncio.run(
                capacity_native_cli(
                    package, session, root, expected_session_id=fixture["session_id"]
                )
            )
            receipt["phases"].append("cli-reopen")
            receipt["memory"] = memory
        # Read only the original prefix: commit must append, never prune history.
        digest = hashlib.sha256()
        remaining = receipt["source_bytes"]
        with session.open("rb") as stream:
            while remaining:
                block = stream.read(min(65536, remaining))
                assert block
                digest.update(block)
                remaining -= len(block)
        assert digest.hexdigest() == before
        assert session.stat().st_size > receipt["source_bytes"]
    finally:
        shutil.rmtree(root)
        receipt["generated_data_removed"] = not root.exists()
        (tmp_path / "capacity-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print("CAPACITY_RESULT " + json.dumps(receipt), flush=True)


def test_prepared_owner_source_refuses_later_bus_correction(session):
    root = session.parent.parent
    registry = Registration(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(session),
            goal=Goal("task", "goal"),
        )
    )
    owner, owner_generation = registry.live_owner_with_generation("owner")
    owner, owner_generation = registry.lease_live_turn_with_generation(
        owner, "turn", expected_owner_generation=owner_generation
    )
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    candidate = bridge.prepare_source(
        owner, owner_generation, settings=PiCompactionSettings(16384, 1), context_window=128000
    )
    assert candidate is not None
    prepared, source = candidate
    before = session.read_bytes()
    comms = Comms(root)
    comms.threads.register(Thread("peer", frozenset(), str(root)))
    comms.messaging.send("peer", "owner", "Retain this corrected requirement")
    with pytest.raises(RelationViolationError, match="source changed"):
        bridge.commit(
            owner,
            owner_generation,
            prepared.witness,
            "Stale summary",
            prepared.tokens_before,
            source=source,
        )
    assert session.read_bytes() == before
    assert bridge.journal.unresolved(str(session)) == ()


@pytest.mark.asyncio
async def test_owner_summary_discards_idle_manager_before_external_native_write(
    session, monkeypatch
):
    root = session.parent.parent
    registry = Registration(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(session),
            goal=Goal("task", "goal"),
        )
    )
    owner, owner_generation = registry.live_owner_with_generation("owner")
    owner, owner_generation = registry.lease_live_turn_with_generation(
        owner, "turn", expected_owner_generation=owner_generation
    )
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    persistent = PersistentPiSession()
    persistent.session_file = str(session)
    persistent.session_id = json.loads(session.read_bytes().splitlines()[0])["id"]
    native_call = bridge._call

    def checked_call(*args, **kwargs):
        assert persistent.proc is None
        assert persistent.reopen_required == str(session)
        assert (
            persistent.reopen_session_id == json.loads(session.read_bytes().splitlines()[0])["id"]
        )
        return native_call(*args, **kwargs)

    monkeypatch.setattr(bridge, "_call", checked_call)

    async def synthetic_summary(metadata):
        assert metadata.witness.session_id == persistent.session_id
        assert persistent.reopen_required is None  # preparation before retirement
        return NativeSummary("Synthetic provider-free summary", None, None)

    result = await compact_owner_once(
        bridge,
        owner,
        owner_generation,
        persistent,
        synthetic_summary,
        settings=PiCompactionSettings(16384, 1),
        context_window=128000,
    )
    assert result is not None and result.state.declared_name == "committed"
    assert persistent.reopen_required == str(session)
    assert len(bridge.journal.pending_publications(str(session))) == 1


@pytest.mark.asyncio
async def test_late_correction_after_summary_refuses_write_without_reusing_manager(session):
    root = session.parent.parent
    registry = Registration(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(session),
            goal=Goal("task", "goal"),
        )
    )
    owner, owner_generation = registry.live_owner_with_generation("owner")
    owner, owner_generation = registry.lease_live_turn_with_generation(
        owner, "turn", expected_owner_generation=owner_generation
    )
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    persistent = PersistentPiSession()
    original = session.read_bytes()
    comms = Comms(root)
    comms.threads.register(Thread("peer", frozenset(), str(root)))

    async def corrected_summary(metadata):
        assert metadata.tokens_before > 0
        comms.messaging.send("peer", "owner", "Correction after preparation")
        return NativeSummary("Now stale", None, None)

    with pytest.raises(RelationViolationError, match="source changed"):
        await compact_owner_once(
            bridge,
            owner,
            owner_generation,
            persistent,
            corrected_summary,
            settings=PiCompactionSettings(16384, 1),
            context_window=128000,
        )
    assert persistent.reopen_required == str(session)
    assert session.read_bytes() == original
    assert bridge.journal.unresolved(str(session)) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["owner", "inner_wrapper", "all_tasks"])
async def test_cancelled_owner_joins_real_native_commit_before_turn_lock_releases(
    session, monkeypatch, shutdown
):
    root = session.parent.parent
    registry = Registration(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(session),
            goal=Goal("task", "goal"),
        )
    )
    owner, owner_generation = registry.live_owner_with_generation("owner")
    owner, owner_generation = registry.lease_live_turn_with_generation(
        owner, "turn", expected_owner_generation=owner_generation
    )
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    persistent = PersistentPiSession()
    native_call = bridge._call
    entered = threading.Event()
    release = threading.Event()
    turn_lock = asyncio.Lock()
    next_entered = asyncio.Event()
    wrappers = []
    wrap = asyncio.wrap_future

    def retained_wrapper(future):
        result = wrap(future)
        wrappers.append(result)
        return result

    monkeypatch.setattr(owner_compaction_runtime.asyncio, "wrap_future", retained_wrapper)

    def delayed_native(*args, **kwargs):
        entered.set()  # Journal intent and owner writer fence already acquired.
        assert release.wait(4), "test did not release native writer"
        return native_call(*args, **kwargs)

    monkeypatch.setattr(bridge, "_call", delayed_native)

    async def synthetic_summary(_metadata):
        return NativeSummary("Synthetic provider-free summary", None, None)

    async def owned_turn():
        async with turn_lock:
            return await compact_owner_once(
                bridge,
                owner,
                owner_generation,
                persistent,
                synthetic_summary,
                settings=PiCompactionSettings(16384, 1),
                context_window=128000,
            )

    task = asyncio.create_task(owned_turn())
    try:
        assert await asyncio.to_thread(entered.wait, 6)
        assert turn_lock.locked()
        assert len(bridge.journal.unresolved(str(session))) == 1
        task.cancel()
        await asyncio.sleep(0.03)
        assert turn_lock.locked() and not task.done()
        task.cancel()  # Even repeated cancellation cannot release the writer scope.
        if shutdown == "inner_wrapper":
            assert len(wrappers) == 1
            wrappers[0].cancel()  # Async wrapper completion is NOT OS worker completion.
        elif shutdown == "all_tasks":
            extra = asyncio.create_task(asyncio.Event().wait(), name="shutdown-bystander")
            for candidate in tuple(asyncio.all_tasks()):
                if candidate is not asyncio.current_task():
                    candidate.cancel()
            await asyncio.gather(extra, return_exceptions=True)
        await asyncio.sleep(0.03)
        assert turn_lock.locked() and not task.done()
        assert len(bridge.journal.unresolved(str(session))) == 1
        assert not any(
            candidate.get_name() == "owner-native-compaction-commit"
            for candidate in asyncio.all_tasks()
        )

        async def next_turn():
            async with turn_lock:
                next_entered.set()

        following = asyncio.create_task(next_turn())
        await asyncio.sleep(0.02)
        assert not next_entered.is_set(), "no next owner input before worker quiescence"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(following, 2)
    assert next_entered.is_set()
    assert not turn_lock.locked()
    assert bridge.journal.unresolved(str(session)) == ()
    pending = bridge.journal.pending_publications(str(session))
    assert len(pending) == 1
    assert bridge.journal.get(pending[0].commit_id).state.declared_name == "committed"
    assert persistent.reopen_required == str(session)
    assert (
        len(
            [
                json.loads(row)
                for row in session.read_bytes().splitlines()
                if json.loads(row).get("type") == "compaction"
            ]
        )
        == 1
    )
    # Only a distinct input with strict validated fresh disk may now proceed.
    torn = session.read_bytes().rstrip(b"\n")
    session.write_bytes(torn)
    started = []

    launcher = "pi"
    comms = Comms(root)
    with comms.bus.log.locked():
        metadata = comms.bus.log.read_metadata_unlocked()
    root_id = (
        metadata.root_id
        if metadata.private
        else comms.messaging.initialize_private_initial_protocol()
    )
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(root))
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", root_id)
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE", PACKAGE)
    start_child = backend.AttachedChild.start

    async def prevent_provider_launch(command, **kwargs):
        if str(Path(PACKAGE) / "dist/cli.js") in command:
            started.append(command)
            raise AssertionError("Corrupt saved session must not launch or send")
        # Read-only A14 validation itself owns a real bounded child.
        return await start_child(command, **kwargs)

    monkeypatch.setattr(backend.AttachedChild, "start", prevent_provider_launch)
    events = [
        event
        async for event in backend.stream_agent_events(
            str(launcher),
            [],
            "distinct input, not replay",
            str(root),
            session_file=str(session),
            persistent_session=persistent,
        )
    ]
    assert events[-1].reason_code == "compaction_reopen_invalid"
    assert not started and session.read_bytes() == torn


def test_three_sequential_native_commits_keep_exact_ids_and_prior_history(session):
    root = session.parent.parent
    registry = Registration(root / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(root),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(session),
            goal=Goal("continuing task", "goal-unchanged"),
        )
    )
    owner, owner_generation = registry.live_owner_with_generation("owner")
    owner, owner_generation = registry.lease_live_turn_with_generation(
        owner, "turn", expected_owner_generation=owner_generation
    )
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    commit_ids = []
    for round_index in range(3):
        if round_index:
            # Test fixture writer, never a production owner path: append a new
            # turn using a fresh native manager after the prior commit returned.
            script = """
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {SessionManager} = await import(pathToFileURL(join(process.argv[1],
  'dist/core/session-manager.js')));
const manager=SessionManager.open(process.argv[2]);
manager.appendMessage({role:'user',content:'Correction round '+process.argv[3]+' '+
  'retain exact goal-unchanged and entry identities '.repeat(300),timestamp:5});
manager.appendMessage({role:'assistant',content:[{type:'text',text:'continued'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:6});
"""
            subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    script,
                    PACKAGE,
                    str(session),
                    str(round_index),
                ],
                capture_output=True,
                check=True,
                timeout=5,
            )
        candidate = bridge.prepare_source(
            owner, owner_generation, settings=PiCompactionSettings(16384, 1), context_window=128000
        )
        assert candidate is not None
        prepared, source = candidate
        operation = bridge.commit(
            owner,
            owner_generation,
            prepared.witness,
            f"Synthetic provider-free round {round_index}; retain goal-unchanged",
            prepared.tokens_before,
            source=source,
        )
        assert operation.state.declared_name == "committed"
        commit_ids.append(operation.commit_id)
        assert bridge.journal.unresolved(str(session)) == ()
        assert registry.require("owner").goal.id == "goal-unchanged"
    assert len(set(commit_ids)) == 3
    assert [
        row.commit_id for row in bridge.journal.pending_publications(str(session))
    ] == commit_ids
    compactions = [
        json.loads(row)
        for row in session.read_bytes().splitlines()
        if json.loads(row).get("type") == "compaction"
    ]
    assert len(compactions) == 3
    assert [row["details"]["agentCommsCommit"]["commitId"] for row in compactions] == commit_ids


@pytest.mark.asyncio
async def test_provider_free_three_round_owner_commit_to_local_acp_metadata(session, tmp_path):
    root = tmp_path / "wire"
    comms = wire(root)
    agent = canonical_agent(comms, agent_bin="pi", runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    agent.inputs.drain_tasks["project"].cancel()
    await asyncio.gather(agent.inputs.drain_tasks["project"], return_exceptions=True)
    comms.threads.attach_session("project", str(session), pid=os.getpid())
    current = comms.registry.require("project")
    comms.registry.register(replace(current, goal=Goal("retain exact history", "goal-e2e")))
    owner, owner_generation = comms.registry.live_owner_with_generation("project")
    owner, owner_generation = comms.registry.lease_live_turn_with_generation(
        owner, "rounds", expected_owner_generation=owner_generation
    )
    bridge = OwnerCompactionCommit(root / "registry.json", Path(PACKAGE))
    persistent = agent.turns.persistent_backends.setdefault("project", PersistentPiSession())
    received = []

    class Client:
        async def session_update(self, session_id, update):
            received.append(update.model_dump(by_alias=True, exclude_none=True))

    agent.on_connect(Client())
    commit_ids = []
    try:
        for index in range(3):
            if index:
                script = """
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {SessionManager} = await import(pathToFileURL(join(process.argv[1],
  'dist/core/session-manager.js')));
const manager=SessionManager.open(process.argv[2]);
manager.appendMessage({role:'user',content:'Correction '+process.argv[3]+' '+
  'retain exact goal-e2e and prior decisions '.repeat(300),timestamp:5});
manager.appendMessage({role:'assistant',content:[{type:'text',text:'continued'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:6});
"""
                subprocess.run(
                    [
                        "node",
                        "--input-type=module",
                        "-e",
                        script,
                        PACKAGE,
                        str(session),
                        str(index),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=5,
                )

            async def synthetic_summary(metadata, round_index=index):
                assert metadata.tokens_before > 0
                return NativeSummary(
                    f"Synthetic round {round_index}; goal-e2e; not a semantic retention claim",
                    None,
                    None,
                )

            operation = await compact_owner_once(
                bridge,
                owner,
                owner_generation,
                persistent,
                synthetic_summary,
                settings=PiCompactionSettings(16384, 1),
                context_window=128000,
            )
            assert operation is not None and operation.state.declared_name == "committed"
            commit_ids.append(operation.commit_id)
            assert persistent.reopen_required == str(session)
            assert await publish_pending_local(agent, "project", "project") == 1
            assert bridge.journal.pending_publications(str(session)) == ()
            assert comms.registry.require("project").goal.id == "goal-e2e"
        publications = [
            fact.publication
            for event in received
            for fact in decode_updates(event.get("_meta"))
            if isinstance(fact, CompactionPublishedUpdate)
        ]
        assert [event.commit_id for event in publications] == commit_ids
        assert len(set(commit_ids)) == 3
        assert "Synthetic round" not in json.dumps(received)
        bus = root / "bus.jsonl"
        assert not bus.exists() or b"Synthetic round" not in bus.read_bytes()
    finally:
        await agent.shutdown()


def test_invalid_session_fails_closed_without_repair(session):
    before = session.read_bytes()
    session.write_bytes(before + b'{"type":"message", broken\n')
    invalid = session.read_bytes()
    with pytest.raises(NativePreparationError):
        prepare_native_source(
            Path(PACKAGE),
            str(session),
            settings=PiCompactionSettings(16384, 1),
            context_window=128000,
        )
    assert session.read_bytes() == invalid


def test_unapproved_session_alias_and_bounds_are_refused(session):
    alias = session.with_name("alias.jsonl")
    alias.symlink_to(session)
    with pytest.raises(NativePreparationError, match="canonical"):
        prepare_native_source(
            Path(PACKAGE),
            str(alias),
            settings=PiCompactionSettings(16384, 1),
            context_window=128000,
        )
    with pytest.raises(NativePreparationError, match="Native source cannot be prepared"):
        prepare_native_source(
            Path(PACKAGE),
            str(session),
            settings=PiCompactionSettings(16384, 0),
            context_window=128000,
        )
