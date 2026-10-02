"""Provider-free selected-tool contract tests; no native Pi/provider launch."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

import pytest

from agent_comms import native_pi
from agent_comms import selected_tool_broker as broker
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import WakeAdmission
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.pi_events import ToolExecutionStart
from agent_comms.pi_payloads import ToolCallContent


def _wire(token: str, **changes: object) -> bytes:
    arguments = {"resource": "notes.txt", "contents": "é", **changes}
    data = {"token": token, "request": {"call_id": "call_1", "arguments": arguments}}
    return (json.dumps(data) + "\n").encode("utf-8")


def test_pre_turn_intent_is_distinct_from_owner_bound_mode() -> None:
    intent = broker.SelectedToolIntent()
    assert type(intent) is broker.SelectedToolIntent
    assert not isinstance(intent, broker.SelectedToolMode)
    assert not hasattr(intent, "action")
    with pytest.raises(TypeError):
        broker.SelectedToolMode(None)  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_default_off_and_packaged_extension_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
    default = native_pi.NativePiRpcLaunch.tracked(tmp_path, worktree=tmp_path, session_dir=sessions)
    assert "--no-tools" in default.argv and "--no-builtin-tools" not in default.argv
    assert "-e" not in default.argv
    mode = broker.SelectedToolMode(lambda _: None)
    (tmp_path / "dist").mkdir()
    packaged = tmp_path / "dist/selected_claimed_write.mjs"
    packaged.write_bytes(Path(broker.__file__).with_name("selected_claimed_write.mjs").read_bytes())
    selected = native_pi.NativePiRpcLaunch.tracked(
        tmp_path, worktree=tmp_path, session_dir=sessions, selected_tool_mode=mode
    )
    assert "--no-tools" not in selected.argv
    assert "--no-builtin-tools" in selected.argv
    assert selected.argv[selected.argv.index("--tools") + 1] == "selected_claimed_write"
    extension = Path(selected.argv[selected.argv.index("-e") + 1])
    assert extension == packaged
    assert not (sessions / "selected-claim-extension.mjs").exists()
    extension.write_text("evil changed extension", encoding="utf-8")
    with pytest.raises(SelectedToolDenied, match="reviewed"):
        native_pi.NativePiRpcLaunch.tracked(
            tmp_path, worktree=tmp_path, session_dir=sessions, selected_tool_mode=mode
        )
    with pytest.raises(native_pi.NativePiUnavailable, match="nominal"):
        native_pi.NativePiRpcLaunch.tracked(
            tmp_path,
            worktree=tmp_path,
            session_dir=sessions,
            selected_tool_mode=object(),  # type: ignore[arg-type]
        )


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_consumption_is_once_only_and_terminal_is_separate(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    input_id = secrets.token_hex(16)
    broker.consume_selected_slot(tmp_path, input_id, "call_1")
    ledger = tmp_path / "selected-tool-ledger"
    assert (ledger / input_id).read_text() == "call_1\n"
    assert not (ledger / (input_id + ".done")).exists()
    with pytest.raises(SelectedToolDenied, match="already consumed"):
        broker.consume_selected_slot(tmp_path, input_id, "call_2")
    broker.record_selected_terminal(tmp_path, input_id, "call_1")
    with pytest.raises(SelectedToolDenied, match="UNKNOWN"):
        broker.record_selected_terminal(tmp_path, input_id, "call_1")


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_unknown_reservation_never_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path.chmod(0o700)
    input_id = secrets.token_hex(16)
    real_sync = broker._sync_dir

    def fail_ledger_only(path: Path) -> None:
        if path.name == "selected-tool-ledger":
            raise OSError("simulated failed directory fsync")
        real_sync(path)

    monkeypatch.setattr(broker, "_sync_dir", fail_ledger_only)
    with pytest.raises(SelectedToolDenied, match="UNKNOWN"):
        broker.consume_selected_slot(tmp_path, input_id, "call_1")
    assert (tmp_path / "selected-tool-ledger" / input_id).exists()
    with pytest.raises(SelectedToolDenied, match="already consumed"):
        broker.consume_selected_slot(tmp_path, input_id, "call_1")


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_owner_mode_denies_unreserved_input_before_consuming_ledger(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)
    comms = Comms(root)
    admission = WakeAdmission(
        wire_root_id="a" * 32,
        source_seq=1,
        source_message_id="source",
        wake_assignment_id="cohort-v1:" + "b" * 64,
        wake_revision=1,
        recipient_lookup="c" * 32,
        execution_id="execution",
        operation_id="d" * 32,
        owner_admission_generation=1,
        turn_id="turn",
        participant_generation=1,
        attempt_ordinal=1,
    )
    with Coordination(str(root / "coordination.sqlite3")) as store:
        install_native_runtime_schema(store)
        input_id = secrets.token_hex(16)
        mode = broker.selected_tool_mode_for_owner(
            comms, store, admission, "owner", session_dir, input_id
        )
        with pytest.raises(SelectedToolDenied, match="exact sent FULL input"):
            mode.action(
                broker.SelectedToolRequest(
                    "call_1", broker.SelectedWriteArguments("notes.txt", "write")
                )
            )
        assert not (session_dir / "selected-tool-ledger").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_writer_order_and_unknown_after_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    input_id = secrets.token_hex(16)
    request = broker.SelectedToolRequest(
        "call_1", broker.SelectedWriteArguments("notes.txt", "replacement")
    )
    trace: list[str] = []

    def publish(*args: object) -> object:
        trace.append("claim")
        assert (tmp_path / "selected-tool-ledger" / input_id).exists()
        return object()

    def write(*args: object) -> None:
        trace.append("write")
        assert trace == ["claim", "write"]

    monkeypatch.setattr(broker, "publish_selected_resource_claim", publish)
    monkeypatch.setattr(broker, "write_selected_claimed_file", write)
    monkeypatch.setattr(broker, "record_selected_terminal", lambda *a: trace.append("terminal"))
    # These dummy owner fields are not production authority; only order is
    # tested here. The typed admission is checked at runtime in this function.
    admission = WakeAdmission(
        wire_root_id="a" * 32,
        source_seq=1,
        source_message_id="source",
        wake_assignment_id="cohort-v1:" + "b" * 64,
        wake_revision=1,
        recipient_lookup="c" * 32,
        execution_id="execution",
        operation_id="d" * 32,
        owner_admission_generation=1,
        turn_id="turn",
        participant_generation=1,
        attempt_ordinal=1,
    )
    broker.perform_selected_write(None, None, admission, "owner", tmp_path, input_id, request)  # type: ignore[arg-type]
    assert trace == ["claim", "write", "terminal"]
    with pytest.raises(SelectedToolDenied, match="already consumed"):
        broker.perform_selected_write(None, None, admission, "owner", tmp_path, input_id, request)  # type: ignore[arg-type]
    assert trace == ["claim", "write", "terminal"]


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
@pytest.mark.parametrize("failed_stage", ["claim_append_unknown", "partial_file_unknown"])
def test_post_reservation_effect_unknown_is_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_stage: str
) -> None:
    tmp_path.chmod(0o700)
    input_id = secrets.token_hex(16)
    request = broker.SelectedToolRequest(
        "call_1", broker.SelectedWriteArguments("notes.txt", "replacement")
    )
    admission = WakeAdmission(
        wire_root_id="a" * 32,
        source_seq=1,
        source_message_id="source",
        wake_assignment_id="cohort-v1:" + "b" * 64,
        wake_revision=1,
        recipient_lookup="c" * 32,
        execution_id="execution",
        operation_id="d" * 32,
        owner_admission_generation=1,
        turn_id="turn",
        participant_generation=1,
        attempt_ordinal=1,
    )
    observed: list[str] = []

    def claim(*_args: object) -> object:
        observed.append("claim")
        if failed_stage == "claim_append_unknown":
            raise OSError("simulated lost bus fsync after visible append")
        return object()

    def write(*_args: object) -> None:
        observed.append("write")
        raise OSError("simulated partial file write")

    monkeypatch.setattr(broker, "publish_selected_resource_claim", claim)
    monkeypatch.setattr(broker, "write_selected_claimed_file", write)
    with pytest.raises(OSError):
        broker.perform_selected_write(None, None, admission, "owner", tmp_path, input_id, request)  # type: ignore[arg-type]
    assert observed == (["claim"] if failed_stage == "claim_append_unknown" else ["claim", "write"])
    ledger = tmp_path / "selected-tool-ledger"
    assert (ledger / input_id).exists()
    assert not (ledger / (input_id + ".done")).exists()
    with pytest.raises(SelectedToolDenied, match="already consumed"):
        broker.perform_selected_write(None, None, admission, "owner", tmp_path, input_id, request)  # type: ignore[arg-type]
    assert observed == (["claim"] if failed_stage == "claim_append_unknown" else ["claim", "write"])


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool socket requires SO_PEERCRED")
def test_fake_socket_requires_pid_token_matching_emitted_call(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    token = secrets.token_hex(32)
    observed: list[broker.SelectedToolRequest] = []

    async def run() -> None:
        server = broker.SelectedToolSocket(tmp_path, token, observed.append)
        await server.start()
        try:
            server.expected_pid = os.getpid() + 100000
            assert not await query(server.path, _wire(token))
            server.expected_pid = os.getpid()
            for changes in (
                {"resource": "../escape"},
                {"resource": "/absolute"},
                {"resource": "@alias"},
                {"resource": "./alias"},
                {"contents": "x" * (128 * 1024 + 1)},
                {"contents": 12},
                {"contents": "\ud800"},
                {"root": "forged"},
            ):
                assert not await query(server.path, _wire(token, **changes))
            assert not observed and not server.calls
            arguments = {"resource": "notes.txt", "contents": "é"}
            pending = asyncio.create_task(query(server.path, _wire(token)))
            await asyncio.sleep(0)
            assert not pending.done()  # Socket arrival is not a native start.
            server.announce(
                [ToolCallContent(id="call_1", name="selected_claimed_write", arguments=arguments)]
            )
            with pytest.raises(SelectedToolDenied):
                server.tool_started(
                    ToolExecutionStart(
                        tool_call_id="call_1",
                        tool_name="selected_claimed_write",
                        args={**arguments, "contents": "different"},
                    )
                )
            server.tool_started(
                ToolExecutionStart(
                    tool_call_id="call_1", tool_name="selected_claimed_write", args=arguments
                )
            )
            assert await pending
            assert not await query(server.path, _wire("0" * 64))
            assert not await query(server.path, _wire(token))  # Consumed exactly once.
            assert observed == [
                broker.SelectedToolRequest(
                    "call_1", broker.SelectedWriteArguments("notes.txt", "é")
                )
            ]
        finally:
            await server.close()
        assert not server.path.exists()

    async def query(path: Path, payload: bytes) -> bool:
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(payload)
        await writer.drain()
        row = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(row)["ok"] is True

    asyncio.run(run())


@pytest.mark.asyncio
async def test_shipped_javascript_tool_uses_authenticated_owner_socket(tmp_path):
    """Execute the actual producer through Node, with no Pi/model/provider."""
    from agent_comms.pi_events import ToolExecutionEnd

    tmp_path.chmod(0o700)
    input_id, token = secrets.token_hex(16), secrets.token_hex(32)
    target = tmp_path / "notes.txt"
    target.write_text("before")

    def commit(request):
        broker.consume_selected_slot(tmp_path, input_id, request.call_id)
        target.write_text(request.arguments.contents)
        broker.record_selected_terminal(tmp_path, input_id, request.call_id)

    socket = broker.SelectedToolSocket(tmp_path, token, commit)
    arguments = {"resource": "notes.txt", "contents": "paired producer é"}
    socket.announce(
        (ToolCallContent(id="paired", name="selected_claimed_write", arguments=arguments),)
    )
    socket.tool_started(
        ToolExecutionStart(
            tool_call_id="paired", tool_name="selected_claimed_write", args=arguments
        )
    )
    await socket.start()
    process = None
    try:
        extension = Path(broker.__file__).with_name("selected_claimed_write.mjs")
        process = await asyncio.create_subprocess_exec(
            "node",
            "--input-type=module",
            "--eval",
            f"""import register from {json.dumps(extension.as_uri())};
let tool;
register({{registerTool(value) {{tool = value}}}});
await tool.execute('paired', {json.dumps(arguments)}, new AbortController().signal);
""",
            env={
                **os.environ,
                "AGENT_COMMS_SELECTED_TOOL_SOCKET": str(socket.path),
                "AGENT_COMMS_SELECTED_TOOL_TOKEN": token,
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        socket.expected_pid = process.pid
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        socket.tool_finished(
            ToolExecutionEnd(
                tool_call_id="paired", tool_name="selected_claimed_write", is_error=False
            ),
            input_id,
        )
        socket.assert_complete()
        assert target.read_text() == arguments["contents"]
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await socket.close()
