"""Credential-free installed-Pi RPC tests: every provider endpoint is loopback-only."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agent_comms import manual_compaction as compact

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
PACKAGE = Path.home() / ".local/pi-npm/lib/node_modules/@earendil-works/pi-coding-agent"


def test_large_saved_session_passes_local_preflight(tmp_path):
    session = tmp_path / "long.jsonl"
    saved_session(session)
    row = json.dumps({"type": "message", "payload": "x" * 4096}).encode() + b"\n"
    with session.open("ab") as stream:
        for _ in range((33 * 1024 * 1024) // len(row) + 1):
            stream.write(row)
    assert compact._session_bytes(session)


def test_codex_model_arguments_are_safe_for_explicit_compaction():
    assert compact._safe_args(["--provider", "openai-codex", "--model", "gpt-5.5"])


def test_private_compaction_profile_carries_codex_auth_without_exposing_it(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text('{"openai-codex":{"type":"oauth","access":"local-fixture"}}')
    source.chmod(0o600)
    profile = compact._private_policy(credentials_source=source)
    try:
        auth = profile / "auth.json"
        assert auth.read_bytes() == source.read_bytes()
        assert auth.stat().st_mode & 0o777 == 0o600
        assert profile.stat().st_mode & 0o777 == 0o700
    finally:
        shutil.rmtree(profile)


def saved_session(path: Path, *, split: bool = False) -> bytes:
    rows = [
        {
            "type": "session",
            "version": 3,
            "id": str(uuid4()),
            "timestamp": "2026-01-01T00:00:00.000Z",
            "cwd": str(path.parent),
        }
    ]
    parent = None
    # Small recent turn keeps the one-request branch; an oversized last assistant
    # induces Pi's two-request split-turn branch, which must be refused pre-RPC.
    content = [
        ("user", "OLD " * 34000),
        ("assistant", "old answer"),
        ("user", "recent request" if split else "RECENT " * 18000),
        ("assistant", "RECENT " * 20000 if split else "recent answer"),
    ]
    for role, text in content:
        entry_id = str(uuid4())
        message = {
            "role": role,
            "content": text if role == "user" else [{"type": "text", "text": text}],
        }
        if role == "assistant":
            message.update(
                provider="openrouter",
                model="fake-compact",
                stopReason="stop",
                usage={
                    "input": 500,
                    "output": 40,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 540,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
                },
            )
        rows.append(
            {
                "type": "message",
                "id": entry_id,
                "parentId": parent,
                "timestamp": "2026-01-01T00:00:00.000Z",
                "message": message,
            }
        )
        parent = entry_id
    raw = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    path.write_bytes(raw)
    return raw


def wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int) -> str:
    """No real Pi launcher, auth store, model catalog, or external URL is used."""
    monkeypatch.setenv("COMPACT_TEST_PORT", str(port))
    monkeypatch.setenv("COMPACT_TEST_PACKAGE", str(PACKAGE))
    monkeypatch.setenv("COMPACT_TEST_PROFILE_CAPTURE", str(tmp_path / "profile"))
    monkeypatch.setenv("COMPACT_TEST_PID_CAPTURE", str(tmp_path / "pi-pid"))
    exe = tmp_path / "pi-local-only"
    exe.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, shutil, stat, sys
profile = pathlib.Path(os.environ['PI_CODING_AGENT_DIR'])
settings = profile / 'settings.json'
assert stat.S_IMODE(profile.stat().st_mode) == 0o700
assert stat.S_IMODE(settings.stat().st_mode) == 0o600
assert not (profile / 'guard.mjs').exists()
assert os.environ['NODE_OPTIONS'] == ''
policy = json.loads(settings.read_text())
assert policy['retry'] == {'enabled': False, 'maxRetries': 0, 'provider': {'maxRetries': 0}}
assert policy['compaction']['enabled'] is False
assert os.environ['PI_OFFLINE'] == '1'
assert sys.argv.count('--session') == 1 and '--no-approve' in sys.argv
assert sys.argv[sys.argv.index('--provider') + 1] == 'openrouter'
assert sys.argv[sys.argv.index('--model') + 1] == 'fake-compact'
port = int(os.environ['COMPACT_TEST_PORT'])
model = {'providers': {'openrouter': {'baseUrl': f'http://127.0.0.1:{port}/v1',
                                     'apiKey': 'local-dummy-not-a-credential',
                                     'api': 'openai-completions',
                                     'models': [{'id': 'fake-compact', 'contextWindow': 128000,
                                                 'maxTokens': 4096, 'reasoning': False,
                                                 'compat': {'supportsUsageInStreaming': False}}]}}}
(profile / 'models.json').write_text(json.dumps(model))
pathlib.Path(os.environ['COMPACT_TEST_PROFILE_CAPTURE']).write_text(str(profile))
pathlib.Path(os.environ['COMPACT_TEST_PID_CAPTURE']).write_text(str(os.getpid()))
cli = pathlib.Path(os.environ['COMPACT_TEST_PACKAGE']) / 'dist/cli.js'
# Never use the user's credential-bearing pi wrapper.
node = shutil.which('node')
assert node is not None
os.execv(node, ['node', str(cli), *sys.argv[1:]])
""")
    exe.chmod(0o700)
    return str(exe)


class LoopbackProvider:
    def __init__(self, *, status: int = 503):
        self.status = status
        self.port = 0
        self.posts = 0
        self.paths = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            lines = head.decode("ascii", errors="replace").split("\r\n")
            if lines[0].startswith("POST "):
                self.posts += 1
                self.paths.append(lines[0])
            length = next(
                (
                    int(line.split(":", 1)[1])
                    for line in lines[1:]
                    if line.lower().startswith("content-length:")
                ),
                0,
            )
            if length:
                await asyncio.wait_for(reader.readexactly(length), 3)
            if self.status == 0:
                # This attempt stays in flight until Pi is cancelled.
                await asyncio.wait_for(reader.read(), 20)
                return
            if self.status == 503:
                body = b'{"error":{"message":"loopback retryable failure","type":"server_error"}}'
                writer.write(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
            else:

                def event(text: str, reason):
                    chunk = {
                        "id": "chatcmpl-local",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fake-compact",
                        "choices": [
                            {"index": 0, "delta": {"content": text}, "finish_reason": reason}
                        ],
                    }
                    return b"data: " + json.dumps(chunk).encode() + b"\n\n"

                body = event("local summary", None) + event("", "stop") + b"data: [DONE]\n\n"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
            await writer.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize("status", [503, 200])
async def test_real_pi_compacts_exact_saved_session(tmp_path, monkeypatch, status):
    provider = LoopbackProvider(status=status)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        provider.port = port
        session = tmp_path / "existing.jsonl"
        before = saved_session(session)
        # The project and user profiles both demand retries. --no-approve and
        # the fsynced private PI_CODING_AGENT_DIR must override both.
        project_settings = tmp_path / ".pi"
        project_settings.mkdir()
        (project_settings / "settings.json").write_text(
            '{"retry":{"enabled":true,"maxRetries":9,"provider":{"maxRetries":9}}}'
        )
        inherited = tmp_path / "inherited-agent"
        inherited.mkdir()
        (inherited / "settings.json").write_text(
            '{"retry":{"enabled":true,"maxRetries":9,"provider":{"maxRetries":9}}}'
        )
        (inherited / "auth.json").write_text('{"sentinel":"not-a-real-credential"}')
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(inherited))
        exe = wrapper(tmp_path, monkeypatch, port)
        # Explicit fake model and loopback URL; no auth.json or inherited creds.
        result = await compact.compact_session(
            exe,
            ["--print", "--provider", "openrouter", "--model", "fake-compact"],
            str(session),
            str(tmp_path),
            timeout_seconds=15,
        )
        assert provider.posts >= 1, (result, provider.paths)
        assert all(path == "POST /v1/chat/completions HTTP/1.1" for path in provider.paths)
        assert not Path((tmp_path / "profile").read_text()).exists()
        assert (inherited / "auth.json").read_text() == '{"sentinel":"not-a-real-credential"}'
        if status == 200:
            assert result["ok"] is True, result
            assert "local summary" in result["summary"]
            assert session.read_bytes().startswith(before)
            assert json.loads(session.read_bytes().splitlines()[-1])["type"] == "compaction"
        else:
            assert result["ok"] is False
            assert session.read_bytes().startswith(before)
            assert all(
                json.loads(row)["type"] != "compaction" for row in session.read_bytes().splitlines()
            )
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("denied", ["session_file", "parent_directory"])
async def test_success_requires_saved_session_file_and_parent_sync(tmp_path, monkeypatch, denied):
    """One model response is not a success receipt if Pi's row cannot be synced."""
    provider = LoopbackProvider(status=200)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    try:
        provider.port = server.sockets[0].getsockname()[1]
        session = tmp_path / "existing.jsonl"
        before = saved_session(session)
        exe = wrapper(tmp_path, monkeypatch, provider.port)
        if denied == "session_file":
            original = os.fsync
            identity = (session.stat().st_dev, session.stat().st_ino)

            def refuse_session_sync(fd):
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) == identity:
                    raise OSError("injected session sync denial")
                return original(fd)

            monkeypatch.setattr(compact.os, "fsync", refuse_session_sync)
        else:
            original_parent = compact._fsync

            def refuse_parent_sync(path):
                if Path(path) == session.parent:
                    raise OSError("injected parent sync denial")
                return original_parent(path)

            monkeypatch.setattr(compact, "_fsync", refuse_parent_sync)
        result = await compact.compact_session(
            exe,
            ["--provider", "openrouter", "--model", "fake-compact"],
            str(session),
            str(tmp_path),
            timeout_seconds=15,
        )
        assert result == {
            "ok": False,
            "error": "Saved compaction durability is uncertain; not retried.",
        }
        assert session.read_bytes().startswith(before)
        assert json.loads(session.read_bytes().splitlines()[-1])["type"] == "compaction"
    finally:
        server.close()
        await server.wait_closed()


async def test_split_turn_can_compact_with_local_provider(tmp_path, monkeypatch):
    provider = LoopbackProvider(status=200)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    try:
        session = tmp_path / "existing.jsonl"
        before = saved_session(session, split=True)
        exe = wrapper(tmp_path, monkeypatch, server.sockets[0].getsockname()[1])
        result = await compact.compact_session(
            exe,
            ["--provider", "openrouter", "--model", "fake-compact"],
            str(session),
            str(tmp_path),
            timeout_seconds=15,
        )
        assert result["ok"] is True, result
        assert provider.posts >= 1
        assert session.read_bytes().startswith(before)
        assert json.loads(session.read_bytes().splitlines()[-1])["type"] == "compaction"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "args",
    [
        ["--no-session"],
        ["--session", "other"],
        ["--"],
        ["--approve"],
        ["--mode", "rpc"],
        ["--provider"],
    ],
)
async def test_unsafe_args_fail_before_child(tmp_path, monkeypatch, args):
    session = tmp_path / "existing.jsonl"
    saved_session(session)
    exe = wrapper(tmp_path, monkeypatch, 1)
    result = await compact.compact_session(exe, args, str(session), str(tmp_path))
    assert result["ok"] is False
    assert not (tmp_path / "profile").exists()


@pytest.mark.parametrize("wrong_session", [False, True])
async def test_rpc_rejects_foreign_id_and_wrong_reopened_session(
    tmp_path,
    monkeypatch,
    wrong_session,
):
    session = tmp_path / "existing.jsonl"
    before = saved_session(session)
    captured = tmp_path / "rpc-commands"
    monkeypatch.setenv("COMPACT_TEST_CAPTURE", str(captured))
    monkeypatch.setenv("COMPACT_TEST_WRONG_SESSION", "1" if wrong_session else "0")
    stub = tmp_path / "pi-foreign-rpc"
    stub.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
path = pathlib.Path(sys.argv[sys.argv.index('--session') + 1])
assert path.is_file()
header = json.loads(path.read_bytes().splitlines()[0])
first = json.loads(sys.stdin.readline())
assert first['type'] == 'get_state'
with open(os.environ['COMPACT_TEST_CAPTURE'], 'a') as log: log.write('get_state\\n')
wrong = os.environ['COMPACT_TEST_WRONG_SESSION'] == '1'
state = {'sessionFile': str(path) + ('-wrong' if wrong else ''),
         'sessionId': header['id'],
         'model': {'provider': 'openrouter', 'id': 'fake-compact',
                   'api': 'openai-completions'}}
print(json.dumps({'id': first['id'], 'type': 'response', 'command': 'get_state',
                  'success': True, 'data': state}), flush=True)
if os.environ['COMPACT_TEST_WRONG_SESSION'] == '0':
    second = json.loads(sys.stdin.readline())
    with open(os.environ['COMPACT_TEST_CAPTURE'], 'a') as log: log.write('compact\\n')
    print(json.dumps({'id': 'foreign', 'type': 'response', 'command': 'compact',
                      'success': True, 'data': {'summary': 'fake'}}), flush=True)
""")
    stub.chmod(0o700)
    result = await compact.compact_session(
        str(stub),
        ["--provider", "openrouter", "--model", "fake-compact"],
        str(session),
        str(tmp_path),
        timeout_seconds=5,
    )
    assert result["ok"] is False
    assert captured.read_text().splitlines() == (
        ["get_state"] if wrong_session else ["get_state", "compact"]
    )
    assert session.read_bytes() == before


@pytest.mark.parametrize("cancel", [False, True])
async def test_uncertain_loopback_post_timeout_or_cancel_never_retries_or_leaks_child(
    tmp_path,
    monkeypatch,
    cancel,
):
    provider = LoopbackProvider(status=0)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    try:
        session = tmp_path / "existing.jsonl"
        saved_session(session)
        exe = wrapper(tmp_path, monkeypatch, server.sockets[0].getsockname()[1])
        task = asyncio.create_task(
            compact.compact_session(
                exe,
                ["--provider", "openrouter", "--model", "fake-compact"],
                str(session),
                str(tmp_path),
                timeout_seconds=3 if not cancel else 10,
            )
        )
        async with asyncio.timeout(5):
            while provider.posts != 1:
                await asyncio.sleep(0.01)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result["ok"] is False
            assert "timed out" in result["error"]
        assert provider.posts == 1
        assert not Path((tmp_path / "profile").read_text()).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int((tmp_path / "pi-pid").read_text()), 0)
    finally:
        server.close()
        await server.wait_closed()


async def test_guard_fsync_failure_stops_before_preflight(tmp_path, monkeypatch):
    session = tmp_path / "existing.jsonl"
    saved_session(session)
    exe = wrapper(tmp_path, monkeypatch, 1)
    original = os.fsync
    calls = 0

    def fail_guard(fd):
        nonlocal calls
        calls += 1
        if calls == 3:  # parent directory, settings.json, then profile directory
            raise OSError("injected profile fsync failure")
        original(fd)

    monkeypatch.setattr(compact.os, "fsync", fail_guard)
    result = await compact.compact_session(
        exe,
        ["--provider", "openrouter", "--model", "fake-compact"],
        str(session),
        str(tmp_path),
    )
    assert calls == 3
    assert result["ok"] is False
    assert not (tmp_path / "profile").exists()


async def test_profile_commit_failure_stops_before_preflight(tmp_path, monkeypatch):
    session = tmp_path / "existing.jsonl"
    saved_session(session)
    exe = wrapper(tmp_path, monkeypatch, 1)

    def fail(_path):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(compact, "_fsync", fail)
    result = await compact.compact_session(
        exe,
        ["--provider", "openrouter", "--model", "fake-compact"],
        str(session),
        str(tmp_path),
    )
    assert result["ok"] is False
    assert not (tmp_path / "profile").exists()
