"""Strict native Pi proof-reader tests; real RPC/provider exercise is separate."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms.native_pi import (
    CAPABILITY,
    NativePiUnavailable,
    _read_native_context_evidence,
    _trusted_package,
    load_native_context_proof,
    prepare_native_pi_rpc_launch,
    run_native_pi_turn,
)

INPUT_ID = "a" * 32
DIGEST = "b" * 64

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="native Pi proof and owner-only UID checks require POSIX"
)


def _evidence(tmp_path: Path) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    session = sessions / "test.jsonl"
    entries = [
        {"type": "session", "id": "sid", "version": 3},
        {
            "type": "message",
            "id": "entry",
            "message": {
                "role": "user",
                "inputId": INPUT_ID,
                "inputDigest": DIGEST,
                "content": [{"type": "text", "text": "separate from its identity"}],
            },
        },
    ]
    journal = [
        {
            "schema": 1,
            "type": "context_committed",
            "sessionId": "sid",
            "inputId": INPUT_ID,
            "sessionEntryId": "entry",
            "requestGeneration": 1,
            "llmContextDigest": "c" * 64,
        }
    ]
    session.write_text("".join(json.dumps(row) + "\n" for row in entries))
    proof = Path(str(session) + ".input-proof")
    proof.write_text("".join(json.dumps(row) + "\n" for row in journal))
    session.chmod(0o600)
    proof.chmod(0o600)
    return session


def test_read_only_journal_parser_is_not_recovery_authority(tmp_path: Path) -> None:
    session = _evidence(tmp_path)
    with pytest.raises(NativePiUnavailable, match="lacks a durable commit marker"):
        load_native_context_proof(session, INPUT_ID)
    first = _read_native_context_evidence(session, INPUT_ID)
    second = _read_native_context_evidence(session, INPUT_ID)
    assert first == second
    assert first.session_id == "sid"
    assert first.session_entry_id == "entry"
    assert first.request_generation == 1
    with pytest.raises(NativePiUnavailable, match="specified input"):
        _read_native_context_evidence(session, "d" * 32)


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate_json_key",
        "missing_newline",
        "world_readable",
        "wrong_entry",
        "duplicate_generation",
        "wrong_session",
        "symlink",
    ],
)
def test_corrupt_or_redirected_journal_cannot_assert_context(tmp_path: Path, damage: str) -> None:
    session = _evidence(tmp_path)
    journal = Path(str(session) + ".input-proof")
    if damage == "duplicate_json_key":
        journal.write_text(journal.read_text().replace('"schema": 1,', '"schema": 1, "schema": 1,'))
    elif damage == "missing_newline":
        journal.write_text(journal.read_text().rstrip("\n"))
    elif damage == "world_readable":
        journal.chmod(0o644)
    elif damage == "wrong_entry":
        journal.write_text(
            journal.read_text().replace('"sessionEntryId": "entry"', '"sessionEntryId": "other"')
        )
    elif damage == "duplicate_generation":
        journal.write_text(journal.read_text() * 2)
    elif damage == "wrong_session":
        journal.write_text(
            journal.read_text().replace('"sessionId": "sid"', '"sessionId": "other"')
        )
    elif damage == "symlink":
        replacement = tmp_path / "replacement"
        replacement.write_text(journal.read_text())
        replacement.chmod(0o600)
        journal.unlink()
        journal.symlink_to(replacement)
    with pytest.raises(NativePiUnavailable):
        _read_native_context_evidence(session, INPUT_ID)


def test_proof_reader_rejects_session_id_duplicate_or_untrusted_ancestor(tmp_path: Path) -> None:
    session = _evidence(tmp_path)
    data = session.read_text()
    user = json.loads(data.splitlines()[1])
    session.write_text(data + json.dumps({**user, "id": "another"}) + "\n")
    with pytest.raises(NativePiUnavailable, match="ambiguous"):
        _read_native_context_evidence(session, INPUT_ID)
    session.write_text(data)
    session.parent.chmod(0o777)
    with pytest.raises(NativePiUnavailable, match="writable ancestor"):
        _read_native_context_evidence(session, INPUT_ID)


@pytest.mark.parametrize("unsafe", ["public_directory", "public_session", "symlink_directory"])
async def test_unprivate_session_rejected_before_pi_process_starts(
    tmp_path: Path, monkeypatch, unsafe: str
) -> None:
    import agent_comms.native_pi as native

    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    selected = sessions
    existing = None
    if unsafe == "public_directory":
        sessions.chmod(0o755)
    elif unsafe == "public_session":
        existing = sessions / "prior.jsonl"
        existing.write_text('{"type":"session","id":"sid"}\n')
        existing.chmod(0o644)
    else:
        selected = tmp_path / "redirected"
        selected.symlink_to(sessions, target_is_directory=True)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("An unsafe Pi session must be refused before process launch")

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(NativePiUnavailable):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="Never disclose input ID",
            worktree=tmp_path,
            session_dir=selected,
            session_file=existing,
        )


@pytest.mark.parametrize(
    ("deny_group_signal", "ignore_term"), [(False, False), (True, False), (True, True)]
)
async def test_tracked_launch_pins_private_no_retry_settings_before_subprocess(
    tmp_path: Path, monkeypatch, deny_group_signal: bool, ignore_term: bool
) -> None:
    import agent_comms.native_pi as native

    project = tmp_path / "project"
    project.mkdir()
    project_settings = project / ".pi"
    project_settings.mkdir()
    (project_settings / "settings.json").write_text(
        json.dumps(
            {
                "retry": {"enabled": True, "provider": {"maxRetries": 9}},
                "compaction": {"enabled": True},
            }
        )
    )
    sessions = tmp_path / "sessions"
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(inherited))
    monkeypatch.setenv("OPENROUTER_API_KEY", "a-token-not-to-persist")
    original = asyncio.create_subprocess_exec
    launches = []
    processes = []
    fake_stub = (
        (
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            if ignore_term
            else ""
        )
        + """import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"type": "response", "id": request["id"],
                  "command": "get_state", "success": False}), flush=True)
"""
        + ("time.sleep(30)\n" if ignore_term else "")
    )

    async def launch(*argv, **kwargs):
        launches.append(argv)
        assert "--no-approve" in argv
        assert all(option in argv for option in ("--no-tools", "--no-extensions", "--no-skills"))
        assert kwargs["env"]["PI_OFFLINE"] == "1"
        assert kwargs["env"]["OPENROUTER_API_KEY"] == "a-token-not-to-persist"
        agent_dir = Path(kwargs["env"]["PI_CODING_AGENT_DIR"])
        assert agent_dir == sessions / ".native-pi-agent"
        assert stat.S_IMODE(agent_dir.stat().st_mode) == 0o700
        policy = agent_dir / "settings.json"
        assert stat.S_ISREG(policy.lstat().st_mode)
        assert stat.S_IMODE(policy.stat().st_mode) == 0o600
        assert policy.read_bytes() == native._NATIVE_SETTINGS
        assert json.loads(policy.read_text()) == {
            "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
            "compaction": {"enabled": False},
        }
        assert b"a-token-not-to-persist" not in policy.read_bytes()
        assert not (agent_dir / "auth.json").exists()
        assert not list(agent_dir.glob(".settings-*.tmp"))
        process = await original(
            sys.executable,
            "-u",
            "-c",
            fake_stub,
            **kwargs,
        )
        processes.append(process)
        return process

    if deny_group_signal:

        def denied_group_signal(_pid, _signal):
            raise PermissionError(errno.EPERM, "injected macOS process-group denial")

        monkeypatch.setattr(native.os, "killpg", denied_group_signal)

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(NativePiUnavailable, match="capability is unavailable"):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="no provider",
            worktree=project,
            session_dir=sessions,
        )
    assert len(launches) == 1
    assert processes[0].returncode is not None
    assert not list(inherited.iterdir())


@pytest.mark.parametrize("failed_fsync", [1, 2, 3])
async def test_any_prelaunch_fsync_failure_denies_subprocess(
    tmp_path: Path, monkeypatch, failed_fsync: int
) -> None:
    import agent_comms.native_pi as native

    actual_fsync = native.os.fsync
    calls = 0

    def fail_selected_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == failed_fsync:
            raise OSError(errno.EIO, "injected policy fsync failure")
        return actual_fsync(descriptor)

    async def forbidden(*_argv, **_kwargs):
        raise AssertionError("No model subprocess may launch without durable retry policy")

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native.os, "fsync", fail_selected_fsync)
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", forbidden)
    sessions = tmp_path / "sessions"
    with pytest.raises(NativePiUnavailable, match="could not be committed"):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="no provider",
            worktree=tmp_path,
            session_dir=sessions,
        )
    assert calls == failed_fsync
    assert not list((sessions / ".native-pi-agent").glob(".settings-*.tmp"))


async def test_visible_policy_after_failed_parent_fsync_is_resynced_before_launch(
    tmp_path: Path, monkeypatch
) -> None:
    import agent_comms.native_pi as native

    original_fsync = native.os.fsync
    settings_syncs = 0
    launches = 0
    sessions = tmp_path / "sessions"
    agent_dir = sessions / ".native-pi-agent"

    def fail_once(descriptor):
        nonlocal settings_syncs
        info = os.fstat(descriptor)
        if agent_dir.exists() and (info.st_dev, info.st_ino) == (
            agent_dir.stat().st_dev,
            agent_dir.stat().st_ino,
        ):
            settings_syncs += 1
            if settings_syncs == 1:
                raise OSError(errno.EIO, "first settings directory fsync failed")
        return original_fsync(descriptor)

    async def record_launch(*_argv, **_kwargs):
        nonlocal launches
        launches += 1
        raise RuntimeError("test reached spawn only after the second complete policy sync")

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native.os, "fsync", fail_once)
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", record_launch)
    request = {
        "input_id": INPUT_ID,
        "prompt": "no provider",
        "worktree": tmp_path,
        "session_dir": sessions,
    }
    with pytest.raises(NativePiUnavailable, match="retry policy could not be committed"):
        await run_native_pi_turn(tmp_path, **request)
    assert launches == 0
    assert (sessions / ".native-pi-agent" / "settings.json").is_file()
    with pytest.raises(RuntimeError, match="second complete policy sync"):
        await run_native_pi_turn(tmp_path, **request)
    assert settings_syncs == 2
    assert launches == 1


def test_nested_session_directory_entries_are_synced_from_leaf_to_private_root(
    tmp_path: Path, monkeypatch
) -> None:
    import agent_comms.native_pi as native

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    nested = root / "nested"
    sessions = nested / "sessions"
    observed: list[Path] = []
    original_fsync = native.os.fsync

    def record_fsync(descriptor):
        info = os.fstat(descriptor)
        for path in (root, nested, sessions, sessions / ".native-pi-agent"):
            if path.exists():
                candidate = path.stat()
                if (info.st_dev, info.st_ino) == (candidate.st_dev, candidate.st_ino):
                    observed.append(path)
                    break
        return original_fsync(descriptor)

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native.os, "fsync", record_fsync)
    launch = prepare_native_pi_rpc_launch(tmp_path, worktree=tmp_path, session_dir=sessions)
    assert launch.session_dir == sessions
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700
    assert stat.S_IMODE(sessions.stat().st_mode) == 0o700
    assert observed.index(root) < observed.index(nested) < observed.index(sessions)
    assert observed.index(sessions) < observed.index(sessions / ".native-pi-agent")
    first_count = len(observed)
    prepare_native_pi_rpc_launch(tmp_path, worktree=tmp_path, session_dir=sessions)
    assert root in observed[first_count:] and nested in observed[first_count:]
    assert observed[first_count:].index(nested) < observed[first_count:].index(sessions)


@pytest.mark.parametrize("failed_parent", ["root", "nested"])
async def test_failed_new_session_parent_sync_denies_spawn_and_retries_visible_entries(
    tmp_path: Path, monkeypatch, failed_parent: str
) -> None:
    import agent_comms.native_pi as native

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    nested = root / "nested"
    sessions = nested / "sessions"
    target = root if failed_parent == "root" else nested
    original_fsync = native.os.fsync
    rejected = False
    successful_target_syncs = 0
    launches = 0

    def fail_once(descriptor):
        nonlocal rejected, successful_target_syncs
        info = os.fstat(descriptor)
        if target.exists():
            current = target.stat()
            if (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino):
                if not rejected:
                    rejected = True
                    raise OSError(errno.EIO, "injected session parent fsync failure")
                successful_target_syncs += 1
        return original_fsync(descriptor)

    async def launch(*_argv, **_kwargs):
        nonlocal launches
        launches += 1
        raise RuntimeError("reached spawn only after re-synchronizing visible directories")

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native.os, "fsync", fail_once)
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", launch)
    request = dict(input_id=INPUT_ID, prompt="no provider", worktree=tmp_path, session_dir=sessions)
    with pytest.raises(NativePiUnavailable, match="session directory could not be committed"):
        await run_native_pi_turn(tmp_path, **request)
    assert launches == 0
    assert rejected and (nested if failed_parent == "root" else sessions).is_dir()
    with pytest.raises(RuntimeError, match="re-synchronizing visible directories"):
        await run_native_pi_turn(tmp_path, **request)
    assert launches == 1
    assert successful_target_syncs >= 1


async def test_reused_session_parent_fsync_failure_denies_spawn(tmp_path: Path, monkeypatch):
    import agent_comms.native_pi as native

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    prepare_native_pi_rpc_launch(tmp_path, worktree=tmp_path, session_dir=sessions)
    original_fsync = native.os.fsync

    def fail_parent(descriptor):
        info = os.fstat(descriptor)
        current = tmp_path.stat()
        if (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino):
            raise OSError(errno.EIO, "reused session directory parent sync failed")
        return original_fsync(descriptor)

    async def forbidden(*_argv, **_kwargs):
        raise AssertionError("Failed reused directory sync must deny subprocess")

    monkeypatch.setattr(native.os, "fsync", fail_parent)
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(NativePiUnavailable, match="session directory could not be committed"):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="no provider",
            worktree=tmp_path,
            session_dir=sessions,
        )


async def test_redirected_private_policy_directory_denies_subprocess(tmp_path: Path, monkeypatch):
    import agent_comms.native_pi as native

    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    redirect = tmp_path / "redirect"
    redirect.mkdir(mode=0o700)
    (sessions / ".native-pi-agent").symlink_to(redirect, target_is_directory=True)

    async def forbidden(*_argv, **_kwargs):
        raise AssertionError("A redirected policy must be rejected before launch")

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(NativePiUnavailable, match="redirected ancestor"):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="no provider",
            worktree=tmp_path,
            session_dir=sessions,
        )


async def test_cancelled_native_turn_reaps_its_real_subprocess(tmp_path: Path, monkeypatch) -> None:
    import agent_comms.native_pi as native

    original = asyncio.create_subprocess_exec
    started = []

    async def launch(*_argv, **kwargs):
        process = await original("/bin/sleep", "30", **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/sleep"))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(
        run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="no output",
            worktree=tmp_path,
            session_dir=tmp_path / "sessions",
            timeout=20,
        )
    )
    for _ in range(100):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)
    assert started[0].returncode is not None


@pytest.mark.skipif(
    sys.platform == "darwin", reason="copied native process group control is Linux-only"
)
@pytest.mark.parametrize("stop_reason", ["stop", "error", "length"])
async def test_context_proof_alone_cannot_validate_a_failed_model_reply(
    tmp_path: Path, monkeypatch, stop_reason: str
) -> None:
    import agent_comms.native_pi as native

    fake = tmp_path / "fake_rpc.py"
    fake.write_text("""import json, os, sys
from pathlib import Path
file = Path(sys.argv[1])
reason = sys.argv[2]
def send(value):
    print(json.dumps(value), flush=True)
state = json.loads(sys.stdin.readline())
send({'type':'response','id':state['id'],'command':'get_state','success':True,
      'data':{'nativeInputProofCapability':'pi-native-input-v1-live-only',
              'sessionId':'sid','sessionFile':str(file)}})
command = json.loads(sys.stdin.readline())
input_id = command['inputId']
entry = {'type':'message','id':'entry','message':{'role':'user',
    'inputId':input_id,'inputDigest':'b'*64}}
file.write_text(json.dumps({'type':'session','id':'sid'})+'\\n'+json.dumps(entry)+'\\n')
proof = {'schema':1,'type':'context_committed','sessionId':'sid',
    'inputId':input_id,'sessionEntryId':'entry','requestGeneration':1,
    'llmContextDigest':'c'*64}
Path(str(file)+'.input-proof').write_text(json.dumps(proof)+'\\n')
os.chmod(file,0o600)
os.chmod(str(file)+'.input-proof',0o600)
send({'type':'response','id':command['id'],'command':'prompt','success':True})
send({'type':'input_committed','sessionId':'sid','inputId':input_id,'sessionEntryId':'entry'})
send(proof)
send({'type':'message_update','assistantMessageEvent':{'type':'text_delta','delta':'IGNORE'}})
send({'type':'message_end','message':{'role':'assistant','stopReason':reason,
    'content':[{'type':'text','text':'IGNORE'}]}})
send({'type':'agent_settled'})
""")
    original = asyncio.create_subprocess_exec

    async def launch(*_argv, **kwargs):
        return await original(
            sys.executable,
            "-u",
            str(fake),
            str(tmp_path / "sessions" / "test.jsonl"),
            stop_reason,
            **kwargs,
        )

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", launch)
    operation = run_native_pi_turn(
        tmp_path,
        input_id=INPUT_ID,
        prompt="Decide IGNORE",
        worktree=tmp_path,
        session_dir=tmp_path / "sessions",
        timeout=3,
    )
    if stop_reason == "stop":
        result = await operation
        assert result.text == "IGNORE"
        assert result.context.input_id == INPUT_ID
    else:
        with pytest.raises(NativePiUnavailable, match="did not finish successfully"):
            await operation


@pytest.mark.skipif(
    sys.platform != "linux", reason="trusted copied package requires a real /var/tmp ancestry"
)
def test_seven_compiled_pins_include_bedrock_and_reject_its_drift(monkeypatch) -> None:
    import agent_comms.native_pi as native

    bedrock = "node_modules/@earendil-works/pi-ai/dist/api/bedrock-converse-stream.js"
    assert native.CAPABILITY == "pi-native-input-v1-live-only"
    expected = {
        "dist/cli.js": "8189b66abc4f9f431dbb70941dcba690d76d040de1fbfff212886be35a53639d",
        "dist/core/agent-session.js": (
            "424066056076cbe2861c12277cb745428b13bf3308a8b9873d3064b62dc740d7"
        ),
        "dist/core/session-manager.js": (
            "dd75fef58eaa5458a91cff9fe1cf70556ebc720afd98720eae3dcb6572cb63ca"
        ),
        "dist/modes/rpc/rpc-mode.js": (
            "6f5438c028032f3bc212b6270a1acf9d3e5d22ce5837b85ff5a6fea6c240c76a"
        ),
        "node_modules/@earendil-works/pi-agent-core/dist/agent.js": (
            "93ed16306399765e79c11f78897f575252d7174b85b81a01cdebb2a479e0e57f"
        ),
        "node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js": (
            "7469aebae3badc125087c55843130fe44c2ea3b68ea1b757c884469dd355d5f4"
        ),
        bedrock: "33c0509b2c64a458079f3a94d6467bd47600acbf28b9b495294053c2b928fb94",
    }
    assert expected == native._PATCHED_SHA
    with TemporaryDirectory(prefix="agent-comms-pi-native-", dir="/var/tmp") as raw:
        package = Path(raw) / "node_modules" / "@earendil-works" / "pi-coding-agent"
        synthetic = {}
        for index, relative in enumerate(native._PATCHED_SHA):
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"synthetic module {index}".encode())
            synthetic[relative] = native.hashlib.sha256(path.read_bytes()).hexdigest()
        monkeypatch.setattr(native, "_PATCHED_SHA", synthetic)
        assert native._trusted_package(package) == package / "dist/cli.js"
        (package / bedrock).write_bytes(b"altered Bedrock compiled module")
        with pytest.raises(NativePiUnavailable, match="differs from reviewed fork"):
            native._trusted_package(package)


def test_obsolete_copied_fork_is_rejected_before_process() -> None:
    old = os.environ.get("AC_NATIVE_OLD_COPIED_PACKAGE")
    if not old:
        pytest.skip("Set AC_NATIVE_OLD_COPIED_PACKAGE for copied-fork negative")
    with pytest.raises(NativePiUnavailable, match="differs from reviewed fork"):
        _trusted_package(Path(old))


async def test_old_live_capability_is_rejected_before_prompt(tmp_path: Path, monkeypatch):
    import agent_comms.native_pi as native

    sent = []

    class Stdin:
        def write(self, raw):
            sent.append(json.loads(raw))

        async def drain(self):
            return None

        def close(self):
            pass

    class Process:
        def __init__(self):
            self.stdin = Stdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = 0
            self.stdout.feed_data(
                (
                    json.dumps(
                        {
                            "type": "response",
                            "id": "native-capability",
                            "command": "get_state",
                            "success": True,
                            "data": {
                                "nativeInputProofCapability": "pi-native-input-v1",
                                "sessionId": "old-session",
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return 0

    async def launch(*_argv, **_kwargs):
        return Process()

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(NativePiUnavailable, match="capability is unavailable"):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="must not be sent to old fork",
            worktree=tmp_path,
            session_dir=tmp_path / "sessions",
        )
    assert sent == [{"type": "get_state", "id": "native-capability"}]


def test_prepared_rpc_launch_requires_exact_package_and_private_policy(
    tmp_path: Path, monkeypatch
) -> None:
    selected = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not selected:
        pytest.skip("Set AC_NATIVE_COPIED_PACKAGE to the reviewed private copied fork")
    worktree = tmp_path / "project"
    worktree.mkdir()
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("PI_AGENT_ID", "must-not-leak")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "wrong-global"))
    launch = prepare_native_pi_rpc_launch(Path(selected), worktree=worktree, session_dir=sessions)
    assert launch.argv[:4] == ("node", str(_trusted_package(Path(selected))), "--mode", "rpc")
    assert "--no-approve" in launch.argv
    assert launch.cwd == worktree
    assert launch.session_dir == sessions
    assert launch.session_file is None
    assert "PI_AGENT_ID" not in launch.env
    assert launch.env["PI_OFFLINE"] == "1"
    assert launch.env["PI_CODING_AGENT_DIR"] == str(sessions / ".native-pi-agent")
    settings = json.loads((sessions / ".native-pi-agent" / "settings.json").read_text())
    assert settings["retry"] == {
        "enabled": False,
        "maxRetries": 0,
        "provider": {"maxRetries": 0},
    }
    assert settings["compaction"]["enabled"] is False


def test_unreviewed_native_transport_fails_before_any_session_side_effect(tmp_path: Path) -> None:
    with pytest.raises(NativePiUnavailable, match="Only the reviewed native OpenRouter model"):
        prepare_native_pi_rpc_launch(
            tmp_path,
            worktree=tmp_path,
            session_dir=tmp_path / "sessions",
            provider="anthropic",
        )
    assert not (tmp_path / "sessions").exists()


@pytest.mark.parametrize("outcome", ["429", "length", "stop"])
async def test_copied_cli_private_policy_allows_one_local_http_attempt(
    tmp_path: Path, monkeypatch, outcome: str
) -> None:
    """Explicit opt-in: real pinned CLI, loopback-only fake provider, no paid key."""
    selected = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not selected:
        pytest.skip("Set AC_NATIVE_COPIED_PACKAGE to the reviewed private copied fork")
    package = Path(selected)
    _trusted_package(package)
    calls: list[str] = []
    chunk = {
        "id": "fixture-length",
        "object": "chat.completion.chunk",
        "created": 12345,
        "model": "z-ai/glm-5.3-flash",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "X"}, "finish_reason": None}
        ],
    }
    terminal = {
        **chunk,
        "choices": [
            {"index": 0, "delta": {}, "finish_reason": outcome if outcome != "429" else "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
    }
    stream_body = (
        "".join(f"data: {json.dumps(value)}\n\n" for value in (chunk, terminal))
        + "data: [DONE]\n\n"
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = (
                b'{"error":{"message":"429 rate limit","type":"rate_limit_error"}}'
                if outcome == "429"
                else stream_body
            )
            self.send_response(429 if outcome == "429" else 200)
            self.send_header(
                "Content-Type", "application/json" if outcome == "429" else "text/event-stream"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        worktree = tmp_path / "project"
        worktree.mkdir(mode=0o700)
        project_config = worktree / ".pi"
        project_config.mkdir(mode=0o700)
        (project_config / "settings.json").write_text(
            json.dumps(
                {
                    "retry": {"enabled": True, "maxRetries": 5, "provider": {"maxRetries": 5}},
                    "compaction": {"enabled": True},
                }
            )
        )
        sessions = tmp_path / "sessions"
        sessions.mkdir(mode=0o700)
        isolated = sessions / ".native-pi-agent"
        isolated.mkdir(mode=0o700)
        catalog = isolated / "models.json"
        catalog.write_text(
            json.dumps(
                {
                    "providers": {
                        "openrouter": {"baseUrl": f"http://127.0.0.1:{server.server_port}/v1"}
                    }
                }
            )
        )
        catalog.chmod(0o600)
        preload = tmp_path / "offline-fetch.cjs"
        prefix = f"http://127.0.0.1:{server.server_port}/"
        preload.write_text(
            "const original=globalThis.fetch;"
            "globalThis.fetch=(url,...rest)=>{"
            "const link=url instanceof Request?url.url:String(url);"
            f"if(!link.startsWith({json.dumps(prefix)})) "
            "throw new Error('BLOCKED_NONLOCAL_NETWORK');"
            "return original(url,...rest);};"
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "offline-fixture-no-real-key")
        monkeypatch.setenv("NODE_OPTIONS", f"--require={preload}")
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "ignored-global"))
        import agent_comms.native_pi as native

        observed: list[str] = []
        rpc_events: list[dict] = []
        real_launch = asyncio.create_subprocess_exec

        class Reader:
            def __init__(self, stream):
                self.stream = stream

            async def readline(self):
                raw = await self.stream.readline()
                if raw:
                    event = json.loads(raw)
                    rpc_events.append(event)
                    observed.append(event["type"])
                return raw

        class Process:
            def __init__(self, underlying):
                self.underlying = underlying
                self.stdin = underlying.stdin
                self.stdout = Reader(underlying.stdout)
                self.stderr = underlying.stderr

            @property
            def returncode(self):
                return self.underlying.returncode

            @property
            def pid(self):
                return self.underlying.pid

            async def wait(self):
                return await self.underlying.wait()

        async def launch(*argv, **kwargs):
            return Process(await real_launch(*argv, **kwargs))

        monkeypatch.setattr(native.asyncio, "create_subprocess_exec", launch)
        request = dict(
            input_id=INPUT_ID,
            prompt="Respond with X, fixture only",
            worktree=worktree,
            session_dir=sessions,
            timeout=15,
        )
        if outcome == "stop":
            result = await run_native_pi_turn(package, **request)
            assert result.text == "X"
            assert result.context.input_id == INPUT_ID
            assert result.context.request_generation == 1
            assert result.context.session_file.parent == sessions
            assert "agent_settled" in observed
        else:
            with pytest.raises(NativePiUnavailable, match="did not finish successfully"):
                await run_native_pi_turn(package, **request)
        assert calls == ["/v1/chat/completions"]
        preflight_index, preflight = next(
            (index, event)
            for index, event in enumerate(rpc_events)
            if event.get("command") == "get_state"
        )
        assert preflight["success"] is True
        assert preflight["data"]["nativeInputProofCapability"] == CAPABILITY
        prompt_index, prompt_ack = next(
            (index, event)
            for index, event in enumerate(rpc_events)
            if event.get("command") == "prompt"
        )
        assert preflight_index < prompt_index
        assert prompt_ack["success"] is True
        committed_input = next(event for event in rpc_events if event["type"] == "input_committed")
        committed_context = next(
            event for event in rpc_events if event["type"] == "context_committed"
        )
        assert committed_input["inputId"] == INPUT_ID
        assert committed_context["inputId"] == INPUT_ID
        assert preflight_index < prompt_index < rpc_events.index(committed_input)
        assert observed.count("context_committed") == 1
        assert "auto_retry_start" not in observed
        assert "compaction_start" not in observed
        proof_files = list(sessions.glob("*.jsonl.input-proof"))
        assert len(proof_files) == 1
        assert len(proof_files[0].read_text().splitlines()) == 1
        assert json.loads(proof_files[0].read_text())["requestGeneration"] == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


async def test_stock_pi_is_rejected_before_any_tracked_prompt(tmp_path: Path, monkeypatch) -> None:
    async def forbidden(*args, **kwargs):
        raise AssertionError("Stock Pi must not be started as a tracked backend")

    monkeypatch.setattr("agent_comms.native_pi.asyncio.create_subprocess_exec", forbidden)
    stock = Path("/home/ts/.local/pi-npm/lib/node_modules/@earendil-works/pi-coding-agent")
    with pytest.raises(NativePiUnavailable, match="Pinned disposable"):
        await run_native_pi_turn(
            stock,
            input_id=INPUT_ID,
            prompt="do not send",
            worktree=tmp_path,
            session_dir=tmp_path / "sessions",
        )
    assert os.geteuid() == os.stat(tmp_path).st_uid
    with pytest.raises(NativePiUnavailable, match="Pinned disposable"):
        _trusted_package(stock)
