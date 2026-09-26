"""Fresh strict saved-session validation after a native manager is discarded."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_comms import backend, manual_compaction_bridge, native_session_reopen
from agent_comms.native_session_reopen import NativeReopenError, validate_native_reopen

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not PACKAGE, reason="Disposable Linux canonical native fixture"
)


@pytest.fixture
def saved(tmp_path):
    assert PACKAGE
    code = """
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const managerURL = pathToFileURL(join(process.argv[1], 'dist/core/session-manager.js'));
const {SessionManager} = await import(managerURL);
const manager = SessionManager.create(process.argv[2], join(process.argv[2], 'sessions'));
manager.appendMessage({role:'user', content:'task', timestamp:1});
manager.appendMessage({role:'assistant',content:[{type:'text',text:'answer'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:2});
console.log(JSON.stringify({id:manager.getSessionId(),file:manager.getSessionFile()}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code, PACKAGE, str(tmp_path)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    item = json.loads(result.stdout)
    assert item["file"] and item["id"]
    launcher = Path(PACKAGE).parents[3] / "bin/pi-native"
    assert launcher.is_file(), launcher
    return launcher, Path(item["file"]), item["id"]


@pytest.mark.asyncio
async def test_cancelled_retirement_keeps_marker_and_reaps_before_next_input(
    saved, tmp_path, monkeypatch
):
    launcher, file, identity = saved
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "print('ready',flush=True);time.sleep(30)",
        start_new_session=True,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    persistent = backend.PersistentPiSession()
    try:
        assert child.stdout is not None
        assert await asyncio.wait_for(child.stdout.readline(), 2) == b"ready\n"
        persistent.proc = child
        persistent.session_file = str(file)
        persistent.session_id = identity
        retiring = asyncio.create_task(persistent.discard_for_external_write(str(file)))
        deadline = asyncio.get_running_loop().time() + 2
        while child.stdin is not None and not child.stdin.is_closing():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.002)
        assert child.returncode is None
        retiring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retiring
        assert persistent.reopen_required == str(file)
        assert persistent.reopen_session_id == identity
        assert persistent._close_task is not None
        # An attempted second borrow cannot escape the in-flight cleanup.
        await persistent.close_idle()
        assert child.returncode is not None
        assert persistent._close_task is None
        assert persistent.reopen_required == str(file)
        corrupt = file.read_bytes().rstrip(b"\n")
        file.write_bytes(corrupt)
        calls = []
        original = native_session_reopen.validate_native_reopen

        def checked(_launcher, session_file, *, expected_session_id):
            calls.append((session_file, expected_session_id))
            return original(str(launcher), session_file, expected_session_id=expected_session_id)

        monkeypatch.setattr(native_session_reopen, "validate_native_reopen", checked)
        spawned = []

        async def forbidden_spawn(*args, **kwargs):
            spawned.append(args)
            raise AssertionError("Invalid saved disk must not launch an RPC child")

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", forbidden_spawn)
        events = [
            event
            async for event in backend.stream_agent_events(
                str(launcher),
                [],
                "not a retry",
                str(tmp_path),
                session_file=str(file),
                persistent_session=persistent,
            )
        ]
        assert events[-1]["reason_code"] == "compaction_reopen_invalid"
        assert calls == [(str(file), identity)] and not spawned
        assert file.read_bytes() == corrupt
    finally:
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGKILL)
            await child.wait()


def test_strict_native_reopen_preserves_bytes_and_strips_preload(saved, tmp_path, monkeypatch):
    launcher, file, identity = saved
    before = file.read_bytes()
    marker = tmp_path / "ambient-marker"
    preload = tmp_path / "ambient.mjs"
    preload.write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(marker))},'unsafe');"
    )
    monkeypatch.setenv("NODE_OPTIONS", f"--import={preload.as_uri()}")
    assert (
        validate_native_reopen(str(launcher), str(file), expected_session_id=identity) == identity
    )
    assert file.read_bytes() == before and not marker.exists()
    with pytest.raises(NativeReopenError, match="identity changed"):
        validate_native_reopen(str(launcher), str(file), expected_session_id="wrong")


@pytest.mark.parametrize("mutation", ["tail", "legacy", "ancestry", "missing", "symlink"])
def test_invalid_disk_never_repaired(saved, tmp_path, mutation):
    launcher, file, identity = saved
    rows = file.read_text().splitlines()
    if mutation == "tail":
        file.write_bytes(file.read_bytes().rstrip(b"\n"))
    elif mutation == "legacy":
        header = json.loads(rows[0])
        header["version"] = 2
        file.write_text("\n".join([json.dumps(header), *rows[1:]]) + "\n")
    elif mutation == "ancestry":
        message = json.loads(rows[1])
        message["parentId"] = "forged"
        file.write_text("\n".join([rows[0], json.dumps(message)]) + "\n")
    elif mutation == "missing":
        file.unlink()
    else:
        alias = tmp_path / "session-link"
        alias.symlink_to(file)
        file = alias
    before = file.read_bytes() if file.exists() else None
    with pytest.raises(NativeReopenError):
        validate_native_reopen(str(launcher), str(file), expected_session_id=identity)
    assert (file.read_bytes() if file.exists() else None) == before


@pytest.mark.asyncio
async def test_canonical_manual_route_cannot_use_installed_legacy_compaction(tmp_path, monkeypatch):
    class Owner:
        _agent_bin = "/disposable/stack/bin/pi-native"
        _turn_locks = {}
        _active_turns = {}

        async def _sync_session_identity(self, session_id):
            return session_id

    async def forbidden(*args, **kwargs):
        raise AssertionError("legacy installed Pi route must not launch")

    monkeypatch.setattr(manual_compaction_bridge.manual_compaction, "compact_session", forbidden)
    result = await manual_compaction_bridge.compact_context(Owner(), "owner")
    assert result == {
        "ok": False,
        "error": "Canonical native compaction requires the owner journal bridge.",
    }


@pytest.mark.asyncio
async def test_renamed_symlink_to_verified_native_cannot_use_legacy_manual_route(
    saved, tmp_path, monkeypatch
):
    launcher, _file, _identity = saved
    alias = tmp_path / "renamed-native"
    alias.symlink_to(launcher)
    assert native_session_reopen.package_for_launcher(str(alias)) == Path(PACKAGE)

    class Owner:
        _agent_bin = str(alias)
        _turn_locks = {}
        _active_turns = {}

        async def _sync_session_identity(self, session_id):
            return session_id

    async def forbidden(*args, **kwargs):
        raise AssertionError("Verified canonical native alias must not reach legacy writer")

    monkeypatch.setattr(manual_compaction_bridge.manual_compaction, "compact_session", forbidden)
    result = await manual_compaction_bridge.compact_context(Owner(), "owner")
    assert result == {
        "ok": False,
        "error": "Canonical native compaction requires the owner journal bridge.",
    }


@pytest.mark.asyncio
async def test_discarded_manager_rechecks_disk_and_rpc_identity_before_prompt(
    saved, tmp_path, monkeypatch
):
    launcher, file, identity = saved
    original = native_session_reopen.validate_native_reopen
    checks = []

    def checked(_stub, session, *, expected_session_id):
        checks.append((session, expected_session_id))
        return original(str(launcher), session, expected_session_id=expected_session_id)

    monkeypatch.setattr(native_session_reopen, "validate_native_reopen", checked)
    marker = tmp_path / "provider-prompt-sent"
    state_identity = tmp_path / "rpc-identity"
    state_identity.write_text("wrong-session")
    stub = tmp_path / "pi-stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,select\n"
        f"from pathlib import Path\n"
        "state=json.loads(sys.stdin.readline())\n"
        f"identity=Path({str(state_identity)!r}).read_text()\n"
        "print(json.dumps({'type':'response','command':'get_state','id':state['id'],"
        "'success':True,'data':{'nativeInputProofCapability':"
        f"{backend.NATIVE_INPUT_CAPABILITY!r},'sessionId':identity,"
        f"'sessionFile':{str(file)!r}}}}}),flush=True)\n"
        "if select.select([sys.stdin],[],[],0.1)[0]:\n"
        f"    Path({str(marker)!r}).write_text(sys.stdin.readline())\n"
    )
    stub.chmod(0o700)
    persistent = backend.PersistentPiSession()
    await persistent.discard_for_external_write(str(file))
    assert persistent.proc is None and persistent.reopen_required == str(file)
    first = [
        event
        async for event in backend.stream_agent_events(
            str(stub),
            [],
            "never send",
            str(tmp_path),
            session_file=str(file),
            persistent_session=persistent,
        )
    ]
    assert first[-1]["ok"] is False and not marker.exists()
    assert persistent.reopen_required == str(file)
    assert checks == [(str(file), None)]
    state_identity.write_text(identity)

    @contextmanager
    def refuse(_public, _native, _text):
        yield False

    second = [
        event
        async for event in backend.stream_agent_events(
            str(stub),
            [],
            "still no provider",
            str(tmp_path),
            session_file=str(file),
            persistent_session=persistent,
            send_boundary=refuse,
        )
    ]
    assert second[-1]["ok"] is False and not marker.exists()
    assert checks == [(str(file), None), (str(file), None)]
    assert persistent.reopen_required == str(file), "Only a settled validated turn clears it"
    before = file.read_bytes()
    file.write_bytes(before.rstrip(b"\n"))
    called = []

    async def no_spawn(*args, **kwargs):
        called.append(args)
        raise AssertionError("Invalid saved disk must not launch Pi")

    monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", no_spawn)
    third = [
        event
        async for event in backend.stream_agent_events(
            str(stub),
            [],
            "do not retry",
            str(tmp_path),
            session_file=str(file),
            persistent_session=persistent,
        )
    ]
    assert third[-1]["reason_code"] == "compaction_reopen_invalid"
    assert not called and not marker.exists()
    assert file.read_bytes() == before.rstrip(b"\n")
