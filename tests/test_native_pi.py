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

from agent_comms.child_process import AttachedChild
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.native_pi import (
    CAPABILITY,
    NativeContextProof,
    NativePiTerminalFailure,
    NativePiUnavailable,
    _trusted_package,
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
    first = NativeContextProof.read_evidence(session, INPUT_ID)
    second = NativeContextProof.read_evidence(session, INPUT_ID)
    assert first == second
    assert first.session_id == "sid"
    assert first.session_entry_id == "entry"
    assert first.request_generation == 1
    with pytest.raises(NativePiUnavailable, match="specified input"):
        NativeContextProof.read_evidence(session, "d" * 32)


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
        NativeContextProof.read_evidence(session, INPUT_ID)


def test_proof_reader_rejects_session_id_duplicate_or_untrusted_ancestor(tmp_path: Path) -> None:
    session = _evidence(tmp_path)
    data = session.read_text()
    user = json.loads(data.splitlines()[1])
    session.write_text(data + json.dumps({**user, "id": "another"}) + "\n")
    with pytest.raises(NativePiUnavailable, match="ambiguous"):
        NativeContextProof.read_evidence(session, INPUT_ID)
    session.write_text(data)
    session.parent.chmod(0o777)
    with pytest.raises(NativePiUnavailable, match="writable ancestor"):
        NativeContextProof.read_evidence(session, INPUT_ID)


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
    monkeypatch.setattr(AttachedChild, "start", forbidden)
    with pytest.raises(NativePiUnavailable):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="Never disclose input ID",
            worktree=tmp_path,
            session_dir=selected,
            session_file=existing,
        )


async def test_tracked_launch_pins_private_no_retry_settings_before_subprocess(
    tmp_path: Path, monkeypatch
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
    original = AttachedChild.start
    launches = []
    processes = []
    fake_stub = """import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"type": "response", "id": request["id"],
                  "command": "get_state", "success": False}), flush=True)
"""

    async def launch(argv, **kwargs):
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
        process = await original((sys.executable, "-u", "-c", fake_stub), **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(AttachedChild, "start", launch)
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
    monkeypatch.setattr(AttachedChild, "start", forbidden)
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
    monkeypatch.setattr(AttachedChild, "start", record_launch)
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
    monkeypatch.setattr(AttachedChild, "start", launch)
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
    monkeypatch.setattr(AttachedChild, "start", forbidden)
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
    monkeypatch.setattr(AttachedChild, "start", forbidden)
    with pytest.raises(NativePiUnavailable, match="redirected ancestor"):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="no provider",
            worktree=tmp_path,
            session_dir=sessions,
        )


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_native_turn_reaps_its_real_subprocess(
    tmp_path: Path, monkeypatch, cancellations: int
) -> None:
    import agent_comms.native_pi as native

    original = AttachedChild.start
    started = []

    async def launch(*_argv, **kwargs):
        process = await original(
            (
                sys.executable,
                "-u",
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                "print('ready',flush=True); time.sleep(30)",
            ),
            **kwargs,
        )
        assert await process.stdout.readline() == b"ready\n"
        started.append(process)
        return process

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/sleep"))
    monkeypatch.setattr(AttachedChild, "start", launch)
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
    for _ in range(cancellations - 1):
        await asyncio.sleep(0.1)
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
    original = AttachedChild.start

    async def launch(*_argv, **kwargs):
        return await original(
            (
                sys.executable,
                "-u",
                str(fake),
                str(tmp_path / "sessions" / "test.jsonl"),
                stop_reason,
            ),
            **kwargs,
        )

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(AttachedChild, "start", launch)
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
def test_compiled_pins_include_model_services_and_bedrock_and_reject_drift(monkeypatch) -> None:
    import agent_comms.native_pi as native

    assert native.CAPABILITY == "pi-native-input-v1-live-only"
    from agent_comms import native_package

    with TemporaryDirectory(prefix="agent-comms-pi-native-", dir="/var/tmp") as raw:
        package = Path(raw) / "node_modules" / "@earendil-works" / "pi-coding-agent"
        package.mkdir(parents=True)
        module = package / "unlisted-dependency.js"
        module.write_bytes(b"reviewed dependency")
        manifest = Path(raw) / "manifest"
        manifest.write_text(
            native_package.TREE_PREFIX + native_package.package_tree_digest(package) + "\n"
        )
        monkeypatch.setattr(native_package, "MANIFEST", manifest)
        assert native._trusted_package(package) == package / "dist/cli.js"
        module.write_bytes(b"altered dependency outside former short hash list")
        with pytest.raises(NativePiUnavailable, match="differs from reviewed fork"):
            native._trusted_package(package)


def test_obsolete_copied_fork_is_rejected_before_process() -> None:
    old = os.environ.get("AC_NATIVE_OLD_COPIED_PACKAGE")
    if not old:
        pytest.skip("Set AC_NATIVE_OLD_COPIED_PACKAGE for copied-fork negative")
    with pytest.raises(NativePiUnavailable, match="differs from reviewed fork"):
        _trusted_package(Path(old))


@pytest.mark.parametrize("alias", ["original", "copy", "hardlink", "with_message"])
async def test_selected_header_marker_denies_path_only_legacy_reopen_before_spawn(
    tmp_path: Path, monkeypatch, alias: str
) -> None:
    import agent_comms.native_pi as native

    fresh = create_fresh_private_session(
        tmp_path / "sessions", worktree=tmp_path, selected_thinking_level="high"
    )
    file = fresh.path
    if alias == "copy":
        file = fresh.path.parent / "copy.jsonl"
        file.write_bytes(fresh.path.read_bytes())
        file.chmod(0o600)
    elif alias == "hardlink":
        file = fresh.path.parent / "alias.jsonl"
        os.link(fresh.path, file)
    elif alias == "with_message":
        with file.open("a") as stream:
            stream.write(json.dumps({"type": "message", "id": "00000001"}) + "\n")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("marked selected source must not spawn via legacy path")

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(AttachedChild, "start", forbidden)
    with pytest.raises(NativePiUnavailable, match="cannot reopen without exact first-start token"):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="do not send",
            worktree=tmp_path,
            session_dir=file.parent,
            session_file=file,
        )
    if alias in {"original", "copy"}:
        fresh.verify_prewrite()
    elif alias == "with_message":
        fresh.verify_saved_identity()


async def test_selected_first_source_is_default_off_before_any_real_cli_spawn(
    tmp_path: Path, monkeypatch
) -> None:

    fresh = create_fresh_private_session(
        tmp_path / "sessions", worktree=tmp_path, selected_thinking_level="high"
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unreviewed selected CLI must never spawn")

    monkeypatch.setattr(AttachedChild, "start", forbidden)
    with pytest.raises(NativePiUnavailable, match="builtins are unreviewed"):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="do not send",
            worktree=tmp_path,
            session_dir=fresh.path.parent,
            session_file=fresh.path,
            maintenance_root=tmp_path,
            prompt_send_boundary=lambda _: None,
            fresh_selected=fresh,
        )
    fresh.verify_prewrite()


@pytest.mark.parametrize(
    "damage", ["low_runtime", "wrong_model", "wrong_session", "changed_inode", "valid_preflight"]
)
async def test_selected_first_source_get_state_fences_runtime_before_raw_prompt(
    tmp_path: Path, monkeypatch, damage: str
) -> None:
    """Fake RPC only; no real CLI factory, provider, prompt, or terminal authority."""
    import agent_comms.native_pi as native

    fresh = create_fresh_private_session(
        tmp_path / "sessions", worktree=tmp_path, selected_thinking_level="high"
    )
    received = tmp_path / "preflight.json"
    start = AttachedChild.start
    launched: list[tuple[str, ...]] = []
    launch_envs: list[dict[str, str]] = []
    boundary_seen = False

    async def launch(argv, **kwargs):
        launched.append(argv)
        launch_envs.append(kwargs["env"])
        if damage == "changed_inode":
            # Keep the original inode allocated; unlink may immediately reuse it.
            fresh.path.rename(fresh.path.with_suffix(".original"))
            fresh.path.write_text('{"type":"session","id":"replacement"}\n')
            fresh.path.chmod(0o600)
        else:
            for row in (
                {
                    "type": "model_change",
                    "id": "f0f0f001",
                    "parentId": fresh.bootstrap_leaf_id,
                    "timestamp": "2026-09-26T00:00:00.000Z",
                    "provider": "openrouter",
                    "modelId": "z-ai/glm-5.3-flash",
                },
                {
                    "type": "thinking_level_change",
                    "id": "f0f0f002",
                    "parentId": "f0f0f001",
                    "timestamp": "2026-09-26T00:00:00.000Z",
                    "thinkingLevel": "high",
                },
            ):
                with fresh.path.open("a") as stream:
                    stream.write(json.dumps(row, separators=(",", ":")) + "\n")
        state = {
            "type": "response",
            "id": "native-capability",
            "command": "get_state",
            "success": True,
            "data": {
                "nativeInputProofCapability": CAPABILITY,
                "sessionId": fresh.session_id,
                "sessionFile": str(fresh.path),
                "model": {"provider": "openrouter", "id": "z-ai/glm-5.3-flash"},
                "thinkingLevel": "high",
                "messageCount": 0,
                "pendingMessageCount": 0,
                "isStreaming": False,
                "isCompacting": False,
            },
        }
        data = state["data"]
        if damage == "low_runtime":
            data["thinkingLevel"] = "low"
        elif damage == "wrong_model":
            data["model"]["id"] = "unreviewed/model"
        elif damage == "wrong_session":
            data["sessionId"] = "other-session"
        program = (
            "import json,sys; from pathlib import Path; "
            "request=json.loads(sys.stdin.readline()); "
            f"Path({str(received)!r}).write_text(json.dumps(request)); "
            f"print({json.dumps(state)!r},flush=True); sys.stdin.read()"
        )
        return await start((sys.executable, "-u", "-c", program), **kwargs)

    class BoundaryReachedError(RuntimeError):
        pass

    def boundary(_file, revision):
        nonlocal boundary_seen
        assert _file == fresh.path
        assert revision == fresh.verify_selected_startup()
        boundary_seen = True
        raise BoundaryReachedError("fake prewrite boundary reached; never send prompt")

    async def fake_prompt_send(_stdin, _payload, admission, *, timeout):
        assert timeout > 0
        admission()  # The real raw writer is deliberately never entered.

    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-ambient-sentinel-not-a-key")
    monkeypatch.setenv("HTTP_PROXY", "fake-ambient-proxy-sentinel")
    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(native, "_require_reviewed_selected_source_cli", lambda: None)
    monkeypatch.setattr(AttachedChild, "start", launch)
    monkeypatch.setattr(native, "send_fenced_prompt", fake_prompt_send)
    monkeypatch.setattr(native.MaintenanceBarrier, "assert_open_unlocked", lambda _: None)
    reason = (
        "fake prewrite boundary"
        if damage == "valid_preflight"
        else "saved inode changed"
        if damage == "changed_inode"
        else "runtime or inode differs"
    )
    expected = BoundaryReachedError if damage == "valid_preflight" else NativePiUnavailable
    with pytest.raises(expected, match=reason):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="never send this fake prompt",
            worktree=tmp_path,
            session_dir=fresh.path.parent,
            session_file=fresh.path,
            maintenance_root=tmp_path,
            prompt_send_boundary=boundary,
            fresh_selected=fresh,
        )
    assert len(launched) == 1
    argv = launched[0]
    assert argv[argv.index("--provider") + 1] == "openrouter"
    assert argv[argv.index("--model") + 1] == "z-ai/glm-5.3-flash"
    assert argv[argv.index("--thinking") + 1] == "high"
    assert "--no-prompt-templates" in argv and "--no-themes" in argv
    assert len(launch_envs) == 1
    assert set(launch_envs[0]) == {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "TMPDIR",
        "PI_OFFLINE",
        "PI_CODING_AGENT_DIR",
        "AGENT_COMMS_SELECTED_SOURCE_COPY",
    }
    assert launch_envs[0]["AGENT_COMMS_SELECTED_SOURCE_COPY"] == "1"
    assert launch_envs[0]["PI_OFFLINE"] == "1"
    assert json.loads(received.read_text()) == {"type": "get_state", "id": "native-capability"}
    assert boundary_seen is (damage == "valid_preflight")


async def test_untracked_capability_is_rejected_before_prompt(tmp_path: Path, monkeypatch):
    import agent_comms.native_pi as native

    start = AttachedChild.start
    received = tmp_path / "preflight.json"
    program = """import json,sys
from pathlib import Path
request=json.loads(sys.stdin.readline())
Path(sys.argv[1]).write_text(json.dumps(request))
print(json.dumps({"type":"response","id":request["id"],"command":"get_state",
                  "success":True,"data":{"nativeInputProofCapability":"untracked",
                  "sessionId":"session"}}),flush=True)
sys.stdin.read()
"""

    async def launch(_argv, **kwargs):
        return await start((sys.executable, "-u", "-c", program, str(received)), **kwargs)

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(AttachedChild, "start", launch)
    with pytest.raises(NativePiUnavailable, match="capability is unavailable"):
        await run_native_pi_turn(
            tmp_path,
            input_id=INPUT_ID,
            prompt="must not send",
            worktree=tmp_path,
            session_dir=tmp_path / "sessions",
        )
    assert json.loads(received.read_text()) == {"type": "get_state", "id": "native-capability"}


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


@pytest.mark.parametrize("provider,model", [("", "model"), ("provider", ""), ("--bad", "model")])
def test_missing_native_model_fails_before_any_session_side_effect(
    tmp_path: Path, provider: str, model: str
) -> None:
    with pytest.raises(NativePiUnavailable, match="explicit provider and model"):
        prepare_native_pi_rpc_launch(
            tmp_path,
            worktree=tmp_path,
            session_dir=tmp_path / "sessions",
            provider=provider,
            model=model,
        )
    assert not (tmp_path / "sessions").exists()


@pytest.fixture
def durable_attempt(tmp_path):
    from agent_comms.coordination import ExecutionOrigin
    from agent_comms.coordination_store import MutationStore, prepare_fence_token
    from agent_comms.durable_turn import DurableTurn

    with MutationStore(tmp_path / "attempt.sqlite3") as store:
        store.register_participant("owner", "owner", "owner", committed=True)
        created = store.create_execution("e", ExecutionOrigin.ACP, "owner", "owner", 1).value
        pending = store.mark_pending("e", expected_revision=created.execution.revision).value
        started = store.start_attempt(
            "e",
            1,
            "owner",
            1,
            prepare_fence_token(),
            expected_execution_revision=pending.execution.revision,
            expected_pointer_revision=pending.pointer_revision,
        ).value
        yield DurableTurn(store, started.fence, started.snapshot.pointer_revision, INPUT_ID)


@pytest.mark.parametrize("outcome", ["429", "length", "stop", "configured"])
async def test_copied_cli_private_policy_allows_one_local_http_attempt(
    tmp_path: Path, monkeypatch, outcome: str, durable_attempt
) -> None:
    """Explicit opt-in: real pinned CLI, loopback-only fake provider, no paid key."""
    selected = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not selected:
        pytest.skip("Set AC_NATIVE_COPIED_PACKAGE to the reviewed private copied fork")
    package = Path(selected)
    _trusted_package(package)
    provider = "configured-fixture" if outcome == "configured" else "openrouter"
    model = "fixture-model" if outcome == "configured" else "z-ai/glm-5.3-flash"
    calls: list[str] = []
    chunk = {
        "id": "fixture-length",
        "object": "chat.completion.chunk",
        "created": 12345,
        "model": model,
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "X"}, "finish_reason": None}
        ],
    }
    terminal = {
        **chunk,
        "choices": [
            {"index": 0, "delta": {}, "finish_reason": "length" if outcome == "length" else "stop"}
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
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            assert request["model"] == model
            assert self.headers["Authorization"] == "Bearer canonical-offline-fixture"
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
        canonical = tmp_path / "canonical-agent-config"
        canonical.mkdir(mode=0o700)
        catalog = canonical / "models.json"
        catalog.write_text(
            json.dumps(
                {
                    "providers": {
                        provider: {
                            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                            "api": "openai-completions",
                            "models": [
                                {
                                    "id": model,
                                    "name": "Offline fixture",
                                    "contextWindow": 8192,
                                    "maxTokens": 128,
                                }
                            ],
                        }
                    }
                }
            )
        )
        catalog.chmod(0o600)
        (canonical / "auth.json").write_text(
            json.dumps({provider: {"type": "api_key", "key": "canonical-offline-fixture"}})
        )
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
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("NODE_OPTIONS", f"--require={preload}")
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(canonical))

        observed: list[str] = []
        rpc_events: list[dict] = []
        real_launch = AttachedChild.start

        class Reader:
            def __init__(self, stream):
                self.stream = stream

            async def readuntil(self, separator):
                raw = await self.stream.readuntil(separator)
                if raw:
                    event = json.loads(raw)
                    rpc_events.append(event)
                    observed.append(event["type"])
                return raw

        async def launch(argv, **kwargs):
            child = await real_launch(argv, **kwargs)
            child.stdout = Reader(child.stdout)
            return child

        monkeypatch.setattr(AttachedChild, "start", launch)
        durable_phases = []

        async def observe(event):
            await durable_attempt.dispatch(event)
            attempt = durable_attempt.store.snapshot("e").attempt
            durable_phases.append(attempt.lifecycle.declared_name)
            assert not attempt.lifecycle.backend_done and not attempt.lifecycle.process_dead

        request = dict(
            observe_event=observe,
            input_id=INPUT_ID,
            prompt="Respond with X, fixture only",
            worktree=worktree,
            session_dir=sessions,
            timeout=15,
            provider=provider,
            model=model,
        )
        if outcome in {"stop", "configured"}:
            result = await run_native_pi_turn(package, **request)
            assert "prompt_accepted" in durable_phases
            assert "model_running" in durable_phases
            durable_attempt.finish()
            assert durable_attempt.store.snapshot("e").attempt.lifecycle.backend_done
            assert result.text == "X"
            assert result.context.input_id == INPUT_ID
            assert result.context.request_generation == 1
            assert result.context.session_file.parent == sessions
            assert "agent_settled" in observed
        else:
            failure = "429 rate limit" if outcome == "429" else "did not finish successfully"
            with pytest.raises(NativePiTerminalFailure, match=failure) as failed:
                await run_native_pi_turn(package, **request)
            assert failed.value.context.input_id == INPUT_ID
            assert "agent_settled" in observed
        assert calls == ["/v1/chat/completions"]
        assert not (sessions / ".native-pi-agent" / "auth.json").exists()
        assert not (sessions / ".native-pi-agent" / "models.json").exists()
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

    monkeypatch.setattr("agent_comms.child_process.AttachedChild.start", forbidden)
    stock = tmp_path / "stock/node_modules/@earendil-works/pi-coding-agent"
    stock.mkdir(parents=True)
    stock.parents[2].chmod(0o700)
    (stock / "unreviewed.js").write_text("not the pinned package")
    with pytest.raises(NativePiUnavailable, match="differs from reviewed fork"):
        await run_native_pi_turn(
            stock,
            input_id=INPUT_ID,
            prompt="do not send",
            worktree=tmp_path,
            session_dir=tmp_path / "sessions",
        )
    assert os.geteuid() == os.stat(tmp_path).st_uid
    with pytest.raises(NativePiUnavailable, match="differs from reviewed fork"):
        _trusted_package(stock)


def test_native_owner_entrypoint_uses_pinned_package_and_preserves_arguments(tmp_path, monkeypatch):
    from agent_comms import native_pi, private_nk_entrypoint

    launch = private_nk_entrypoint.PrivateNkLaunch(tmp_path, "a" * 32, tmp_path / "pi", None)
    monkeypatch.setattr(private_nk_entrypoint, "private_nk_from_environment", lambda: launch)
    verified = []
    cli = launch.native_package / "dist" / "cli.js"

    def trusted(package):
        verified.append(package)
        return cli

    monkeypatch.setattr(native_pi, "_trusted_package", trusted)
    monkeypatch.setattr(sys, "argv", ["pi-comms-native", "--mode", "rpc", "--model", "owner/model"])
    executed = []
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "canonical"))
    monkeypatch.delenv("AGENT_COMMS_NATIVE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("NODE_OPTIONS", "--import=/unreviewed.js")
    monkeypatch.setattr(
        os, "execvpe", lambda executable, argv, env: executed.append((executable, argv, env))
    )
    assert native_pi.main() == 0
    assert verified == [launch.native_package]
    executable, argv, environment = executed[0]
    assert executable == "node"
    assert argv == [
        "node",
        "--no-global-search-paths",
        "--import",
        str(cli.with_name("agent-comms-import-fence.mjs")),
        "--import",
        str(cli.with_name("agent-comms-project-bootstrap.mjs")),
        str(cli),
        "--mode",
        "rpc",
        "--model",
        "owner/model",
    ]
    assert environment["AGENT_COMMS_NATIVE_CONFIG_DIR"] == str(tmp_path / "canonical")
    assert "NODE_OPTIONS" not in environment


def test_native_owner_entrypoint_refuses_unconfigured_route(monkeypatch):
    from agent_comms import native_pi, private_nk_entrypoint

    monkeypatch.setattr(private_nk_entrypoint, "private_nk_from_environment", lambda: None)
    with pytest.raises(NativePiUnavailable, match="configured private route"):
        native_pi.main()


def test_provider_failure_notice_does_not_broadcast_untrusted_error_body(tmp_path):
    from agent_comms.native_pi import NativeContextProof

    context = NativeContextProof(
        INPUT_ID, "session", "entry", 1, DIGEST, tmp_path / "session.jsonl"
    )
    failed = NativePiTerminalFailure(
        "SECRET provider response including prompt data", context, "provider", "model"
    )
    assert failed.public_message == "provider/model: provider request failed."
    assert "SECRET" not in failed.public_message
    usage = NativePiTerminalFailure(
        "Codex error: The usage limit has been reached", context, "openai-codex", "gpt-6-sol"
    )
    assert "The usage limit has been reached" in usage.public_message


def test_recorded_context_remains_verifiable_after_later_tool_rounds(tmp_path):
    session = _evidence(tmp_path)
    recorded = NativeContextProof.read_evidence(session, INPUT_ID)
    journal = Path(str(session) + ".input-proof")
    first = json.loads(journal.read_text())
    later = {**first, "requestGeneration": 2, "llmContextDigest": "d" * 64}
    with journal.open("a") as output:
        output.write(json.dumps(later) + "\n")
    assert NativeContextProof.read_evidence(session, INPUT_ID).request_generation == 2
    assert NativeContextProof.read_evidence(session, INPUT_ID, request_generation=1) == recorded
    with pytest.raises(NativePiUnavailable, match="no assembled-context"):
        NativeContextProof.read_evidence(session, INPUT_ID, request_generation=3)


def test_retained_proof_beyond_old_reader_quota_preserves_first_generation(tmp_path):
    session = _evidence(tmp_path)
    journal = Path(str(session) + ".input-proof")
    first = json.loads(journal.read_text())
    with journal.open("a") as stream:
        for generation in range(2, 80002):
            stream.write(json.dumps({**first, "requestGeneration": generation}) + "\n")
    assert journal.stat().st_size > 16 * 1024 * 1024
    observed = NativeContextProof.read_evidence(session, INPUT_ID, request_generation=1)
    assert (
        observed.request_generation == 1
        and observed.llm_context_digest == first["llmContextDigest"]
    )
    # Historical corroboration never gains live delivery/acceptance authority.
    assert observed.input_id == INPUT_ID


def test_proof_observation_rejects_file_mutation(tmp_path):
    from agent_comms.native_pi import _read_private_file

    session = _evidence(tmp_path)
    journal = Path(str(session) + ".input-proof")
    rows = _read_private_file(journal)
    assert next(rows)["inputId"] == INPUT_ID
    with journal.open("a") as stream:
        stream.write("\n")
    with pytest.raises(NativePiUnavailable, match="changed during observation"):
        next(rows)
