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
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_store import MutationStore
from agent_comms.envelope_claim_transitions import WakeAdmission
from agent_comms.operations import Comms


def _wire(token: str, **changes: object) -> bytes:
    data = {"token": token, "call_id": "call_1", "resource": "notes.txt", "contents": "é"}
    data.update(changes)
    return (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")


def test_pre_turn_intent_is_distinct_from_owner_bound_mode() -> None:
    intent = broker.SelectedToolIntent()
    assert type(intent) is broker.SelectedToolIntent
    assert not isinstance(intent, broker.SelectedToolMode)
    assert not hasattr(intent, "action")
    with pytest.raises(TypeError):
        broker.SelectedToolMode(None)  # type: ignore[arg-type]


def test_strict_bounded_request_and_no_model_admission() -> None:
    token = secrets.token_hex(32)
    assert broker.parse_selected_request(_wire(token), token) == broker.SelectedToolRequest(
        "call_1", "notes.txt", "é".encode()
    )
    bad = (
        _wire(token, root="forged"),
        _wire(token, resource="../escape"),
        _wire(token, resource="/tmp/absolute"),
        _wire(token, resource="@notes.txt"),
        _wire(token, resource="./notes.txt"),
        _wire(token, contents="x" * (128 * 1024 + 1)),
        _wire("0" * 64),
        _wire(token)[:-1],
        b'{"token":"' + token.encode() + b'","token":"' + token.encode() + b'"}\n',
    )
    for raw in bad:
        with pytest.raises(broker.SelectedToolDenied):
            broker.parse_selected_request(raw, token)


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_default_off_and_pinned_extension_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
    default = native_pi.prepare_native_pi_rpc_launch(
        tmp_path, worktree=tmp_path, session_dir=sessions
    )
    assert "--no-tools" in default.argv and "--no-builtin-tools" not in default.argv
    assert "-e" not in default.argv
    mode = broker.SelectedToolMode(lambda _: None)
    selected = native_pi.prepare_native_pi_rpc_launch(
        tmp_path, worktree=tmp_path, session_dir=sessions, selected_tool_mode=mode
    )
    assert "--no-tools" not in selected.argv
    assert "--no-builtin-tools" in selected.argv
    assert selected.argv[selected.argv.index("--tools") + 1] == "selected_claimed_write"
    extension = Path(selected.argv[selected.argv.index("-e") + 1])
    assert extension == sessions / "selected-claim-extension.mjs"
    assert extension.stat().st_mode & 0o777 == 0o600
    extension.write_text("evil changed extension", encoding="utf-8")
    with pytest.raises(broker.SelectedToolDenied, match="pinned"):
        native_pi.prepare_native_pi_rpc_launch(
            tmp_path, worktree=tmp_path, session_dir=sessions, selected_tool_mode=mode
        )
    with pytest.raises(native_pi.NativePiUnavailable, match="nominal"):
        native_pi.prepare_native_pi_rpc_launch(
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
    with pytest.raises(broker.SelectedToolDenied, match="already consumed"):
        broker.consume_selected_slot(tmp_path, input_id, "call_2")
    broker.record_selected_terminal(tmp_path, input_id, "call_1")
    with pytest.raises(broker.SelectedToolDenied, match="UNKNOWN"):
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
    with pytest.raises(broker.SelectedToolDenied, match="UNKNOWN"):
        broker.consume_selected_slot(tmp_path, input_id, "call_1")
    assert (tmp_path / "selected-tool-ledger" / input_id).exists()
    with pytest.raises(broker.SelectedToolDenied, match="already consumed"):
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
        wake_claim_id="cohort-v1:" + "b" * 64,
        wake_revision=1,
        recipient_lookup="c" * 32,
        execution_id="execution",
        operation_id="d" * 32,
        owner_admission_generation=1,
        turn_id="turn",
        participant_generation=1,
        attempt_ordinal=1,
    )
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_native_runtime_schema(store)
        input_id = secrets.token_hex(16)
        mode = broker.selected_tool_mode_for_owner(
            comms, store, admission, "owner", session_dir, input_id
        )
        with pytest.raises(broker.SelectedToolDenied, match="exact sent FULL input"):
            mode.action(broker.SelectedToolRequest("call_1", "notes.txt", b"write"))
        assert not (session_dir / "selected-tool-ledger").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
def test_writer_order_and_unknown_after_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    input_id = secrets.token_hex(16)
    request = broker.SelectedToolRequest("call_1", "notes.txt", b"replacement")
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
        wake_claim_id="cohort-v1:" + "b" * 64,
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
    with pytest.raises(broker.SelectedToolDenied, match="already consumed"):
        broker.perform_selected_write(None, None, admission, "owner", tmp_path, input_id, request)  # type: ignore[arg-type]
    assert trace == ["claim", "write", "terminal"]


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool storage requires POSIX dirfd")
@pytest.mark.parametrize("failed_stage", ["claim_append_unknown", "partial_file_unknown"])
def test_post_reservation_effect_unknown_is_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_stage: str
) -> None:
    tmp_path.chmod(0o700)
    input_id = secrets.token_hex(16)
    request = broker.SelectedToolRequest("call_1", "notes.txt", b"replacement")
    admission = WakeAdmission(
        wire_root_id="a" * 32,
        source_seq=1,
        source_message_id="source",
        wake_claim_id="cohort-v1:" + "b" * 64,
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
    with pytest.raises(broker.SelectedToolDenied, match="already consumed"):
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
            assert not await query(server.path, _wire(token))  # no Pi start event
            server.approve_tool_start("call_1", {"resource": "notes.txt", "contents": "different"})
            assert not await query(server.path, _wire(token))  # forged arguments
            server.approve_tool_start("call_1", {"resource": "notes.txt", "contents": "é"})
            assert not await query(server.path, _wire("0" * 64))
            assert await query(server.path, _wire(token))
            assert observed == [broker.SelectedToolRequest("call_1", "notes.txt", "é".encode())]
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
