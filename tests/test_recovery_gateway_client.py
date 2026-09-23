"""Gateway-only bounded DTO client tests; no Toad import or coordinator write authority."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from agent_comms.coordination import CoordinationStore
from agent_comms.recovery_gateway import RecoveryGateway
from agent_comms.recovery_gateway_client import read_gateway_projection


@pytest.fixture
def root():
    private = Path(tempfile.mkdtemp(prefix="rg-client-", dir="/var/tmp"))
    private.chmod(0o700)
    try:
        yield private
    finally:
        shutil.rmtree(private)


async def test_existing_gateway_restart_returns_only_redacted_owner_dto(root: Path) -> None:
    with CoordinationStore(root / "coordination.sqlite3") as store:
        db = store._connection
        db.execute("INSERT INTO participants VALUES ('a','Alice',1)")
        db.execute("INSERT INTO owner_generations VALUES ('a','Alice',1)")
        db.execute("INSERT INTO current_executions VALUES ('a',NULL,NULL,0)")
        db.execute(
            "INSERT INTO executions "
            "(execution_id,origin,status,exact_target,owner_thread,owner_lookup,revision,"
            "current_attempt_ordinal,max_attempts,reason_code,created_at_ms,updated_at_ms) "
            "VALUES ('SECRET_EXECUTION','acp','pending',NULL,'Alice','a',1,NULL,2,NULL,1,1)"
        )
    database = root / "coordination.sqlite3"
    initial = (database.stat().st_ino, database.stat().st_size, database.stat().st_mtime_ns)
    gateway = RecoveryGateway(root)
    for _ in range(2):
        await gateway.start()
        try:
            view = await read_gateway_projection(gateway.path, "Alice")
            assert view["availability"] == "available"
            assert view["owner"] == "Alice"
            assert view["current"]["status"] == "pending"
            assert "SECRET_EXECUTION" not in json.dumps(view)
            assert set(view) == {
                "schema",
                "availability",
                "owner",
                "sampledAtMs",
                "current",
                "lastRecovery",
                "connectivity",
            }
            assert await read_gateway_projection(gateway.path, "not-Alice") != view
        finally:
            await gateway.close()
        assert (await read_gateway_projection(gateway.path, "Alice"))[
            "reason"
        ] == "gateway_unavailable"
    assert initial == (database.stat().st_ino, database.stat().st_size, database.stat().st_mtime_ns)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":1,"availability":"available","owner":"Alice","sampledAtMs":0,"current":null,"lastRecovery":null,"connectivity":null,"executionId":"SECRET"}\n',
        b'{"schema":1,"schema":1,"availability":"unavailable","reason":"busy"}\n',
        b'{"schema":1,"availability":"unavailable","reason":[]}\n',
        b'{"schema":1,"availability":"available","owner":"Alice","sampledAtMs":0,"current":{"status":[],"origin":"wire","isCurrent":false,"attempt":null,"canRetry":false,"publication":null},"lastRecovery":null,"connectivity":null}\n',
        b"X" * 4097 + b"\n",
        b"[" * 1400 + b"0" + b"]" * 1400 + b"\n",  # bounded recursion bomb
    ],
)
async def test_malformed_or_overbroad_gateway_reply_fails_closed(root: Path, raw: bytes) -> None:
    directory = root / ".recovery-viewer"
    directory.mkdir(mode=0o700)
    path = directory / "gateway.sock"

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1025)
        writer.write(raw)
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(respond, path=str(path))
    path.chmod(0o600)
    try:
        view = await read_gateway_projection(path, "Alice")
        assert view == {"schema": 1, "availability": "unavailable", "reason": "gateway_unavailable"}
    finally:
        server.close()
        await server.wait_closed()


async def test_no_socket_or_unsafe_path_never_creates_a_gateway(root: Path) -> None:
    path = root / ".recovery-viewer" / "gateway.sock"
    assert (await read_gateway_projection(path, "Alice"))["reason"] == "gateway_unavailable"
    assert list(root.iterdir()) == []
    redirect = root.parent / (root.name + "-redirect")
    redirect.symlink_to(root, target_is_directory=True)
    try:
        assert (
            await read_gateway_projection(redirect / ".recovery-viewer" / "gateway.sock", "Alice")
        )["reason"] == "gateway_unavailable"
    finally:
        redirect.unlink()
    assert (await read_gateway_projection(path, "\u0000private"))["reason"] == "gateway_unavailable"
    assert list(root.iterdir()) == []
