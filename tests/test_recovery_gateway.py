"""The optional gateway is same-UID local scope, never a Pi-owner service."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.recovery_gateway import GatewayUnavailableError, RecoveryGateway

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="local gateway needs Linux SO_PEERCRED and /var/tmp"
)


@pytest.fixture
def private_root():
    # Unix socket paths have a hard byte limit; use a short, owned test root.
    root = Path(tempfile.mkdtemp(prefix="rg-", dir="/var/tmp"))
    try:
        with CoordinationStore(root / "coordination.sqlite3") as store:
            db = store._connection
            db.execute("INSERT INTO participants VALUES ('a','Alice',1)")
            db.execute("INSERT INTO owner_generations VALUES ('a','Alice',1)")
            db.execute(
                "INSERT INTO current_executions "
                "(owner_lookup,execution_id,attempt_ordinal,pointer_revision) "
                "VALUES ('a',NULL,NULL,0)"
            )
            db.execute(
                (
                    "INSERT INTO executions (execution_id,origin,lifecycle,exact_target,owner_thr"
                    "ead,owner_lookup,revision,max_attempts,reason_code,created_at_ms,updated_at_"
                    "ms) VALUES ('secret-execution','acp',json_object('kind','pending'),NULL,'Ali"
                    "ce','a',1,2,NULL,1,1)"
                )
            )
        yield root
    finally:
        shutil.rmtree(root)


async def request(path: Path, raw: bytes = b'{"thread":"Alice"}\n') -> bytes:
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(raw)
        writer.write_eof()  # one bounded line, no pipelined second request
        await writer.drain()
        try:
            return await asyncio.wait_for(reader.read(4097), timeout=2)
        except (BrokenPipeError, ConnectionResetError):
            return b""  # credential rejection may reset an unread request
    finally:
        writer.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await writer.wait_closed()


async def test_snapshot_offline_is_bounded_redacted_and_does_not_start_owner(
    private_root: Path, monkeypatch
):
    from agent_comms.owner_lifecycle import OwnerLifecycle

    def forbidden(*args, **kwargs):
        raise AssertionError("snapshot must never launch Pi or mutate registry")

    monkeypatch.setattr(OwnerLifecycle, "ensure_owner", forbidden)
    monkeypatch.setattr(OwnerLifecycle, "start", forbidden)
    db = private_root / "coordination.sqlite3"
    before = (db.stat().st_ino, db.stat().st_size, db.stat().st_mtime_ns)
    gateway = RecoveryGateway(private_root)
    await gateway.start()
    try:
        assert stat.S_IMODE(gateway.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(gateway.path.stat().st_mode) == 0o600
        assert stat.S_IMODE((gateway.directory / "gateway.lock").stat().st_mode) == 0o600
        result = await request(gateway.path)
        data = json.loads(result)
        assert data["availability"] == "available"
        assert data["owner"] == "Alice"
        assert data["current"]["status"] == "pending"
        assert len(result) <= 4096
        assert "secret-execution" not in result.decode()
        assert not any(
            (private_root / f"coordination.sqlite3{suffix}").exists()
            for suffix in ("-journal", "-wal", "-shm")
        )
        assert (db.stat().st_ino, db.stat().st_size, db.stat().st_mtime_ns) == before
    finally:
        await gateway.close()
    assert not gateway.path.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"{}\n",
        b'{"thread":true}\n',
        b'{"thread":null}\n',
        b'{"thread":"Alice","thread":"Bob"}\n',
        b'{"thread":"Alice","owner_lookup":"a"}\n',
        b'{"thread":"Alice","wireRoot":"/tmp"}\n',
        b'{"thread":"Alice"}\n{"thread":"Alice"}\n',
        b'{"thread":"Alice"}',
        b'{"thread":"\\u0000"}\n',
        b"\xff\n",
        b"x" * 1025,
    ],
)
async def test_untrusted_request_is_one_exact_bounded_utf8_line(private_root: Path, raw: bytes):
    gateway = RecoveryGateway(private_root)
    await gateway.start()
    try:
        result = json.loads(await request(gateway.path, raw))
        assert result == {
            "schema": 1,
            "availability": "unavailable",
            "reason": "gateway_unavailable",
        }
    finally:
        await gateway.close()


async def test_slow_request_and_excess_clients_are_bounded(private_root: Path):
    gateway = RecoveryGateway(private_root)
    await gateway.start()
    clients = []
    try:
        for _ in range(8):
            reader, writer = await asyncio.open_unix_connection(gateway.path)
            writer.write(b"{")  # hold a partial request without EOF
            await writer.drain()
            clients.append((reader, writer))
        await asyncio.sleep(0.05)
        assert gateway._clients.locked()
        assert await request(gateway.path) == b""  # rejected, not queued
        # The shared 1s deadline releases a stalled client with a generic error.
        answer = await asyncio.wait_for(clients[0][0].read(4096), timeout=2)
        assert json.loads(answer)["reason"] == "gateway_unavailable"
    finally:
        for _reader, writer in clients:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        await gateway.close()


async def test_peer_rejected_before_any_request_parse_or_sql(private_root: Path, monkeypatch):
    import agent_comms.recovery_gateway as module

    def forbidden(*args, **kwargs):
        raise AssertionError("must reject credentials before parsing or opening DB")

    monkeypatch.setattr(module, "_peer_uid", lambda sock: os.geteuid() + 1)
    monkeypatch.setattr(module, "_decode_request", forbidden)
    monkeypatch.setattr(module, "_snapshot", forbidden)
    gateway = RecoveryGateway(private_root)
    await gateway.start()
    try:
        assert await request(gateway.path, b"not-json\n") == b""
    finally:
        await gateway.close()


async def test_unknown_alias_and_ambiguous_owner_are_generic(private_root: Path):
    gateway = RecoveryGateway(private_root)
    await gateway.start()
    try:
        db = private_root / "coordination.sqlite3"
        with sqlite3.connect(db) as connection:
            connection.execute("INSERT INTO participant_aliases VALUES ('alias','a',1)")
        assert json.loads(await request(gateway.path, b'{"thread":"alias"}\n'))["reason"] == (
            "gateway_unavailable"
        )
        with sqlite3.connect(db) as connection:
            connection.execute("INSERT INTO participants VALUES ('b','Alice',1)")
            connection.execute("INSERT INTO owner_generations VALUES ('b','Alice',1)")
        assert json.loads(await request(gateway.path))["reason"] == "gateway_unavailable"
    finally:
        await gateway.close()


async def test_over_budget_is_bounded_before_reader_sort(private_root: Path):
    gateway = RecoveryGateway(private_root)
    await gateway.start()
    try:
        with sqlite3.connect(private_root / "coordination.sqlite3") as connection:
            # The 257th record is refused *before* the frozen reader sorts.
            connection.executemany(
                (
                    "INSERT INTO executions (execution_id,origin,lifecycle,exact_target,owner_thr"
                    "ead,owner_lookup,revision,max_attempts,reason_code,created_at_ms,updated_at_"
                    "ms) VALUES (?,'acp',json_object('kind','pending'),NULL,'Alice','a',1,2,NULL,"
                    "1,1)"
                ),
                ((f"many-{index}",) for index in range(256)),
            )
        assert json.loads(await request(gateway.path))["reason"] == "gateway_unavailable"
    finally:
        await gateway.close()


async def test_registered_owner_scan_is_bounded(private_root: Path):
    gateway = RecoveryGateway(private_root)
    await gateway.start()
    try:
        with sqlite3.connect(private_root / "coordination.sqlite3") as connection:
            connection.executemany(
                "INSERT INTO participants VALUES (?, ?, 1)",
                ((f"owner-{index}", f"Worker-{index}") for index in range(256)),
            )
            connection.executemany(
                "INSERT INTO owner_generations VALUES (?, ?, 1)",
                ((f"owner-{index}", f"Worker-{index}") for index in range(256)),
            )
        assert json.loads(await request(gateway.path))["reason"] == "gateway_unavailable"
    finally:
        await gateway.close()


async def test_single_instance_safe_stale_socket_and_live_foreign_rejection(private_root: Path):
    gateway = RecoveryGateway(private_root)
    await gateway.start()
    try:
        duplicate = RecoveryGateway(private_root)
        with pytest.raises((GatewayUnavailableError, OSError)):
            await duplicate.start()
        assert gateway.path.is_socket()
        assert json.loads(await request(gateway.path))["availability"] == "available"
    finally:
        await gateway.close()
    # A socket inode with no listener can be removed only under the instance lock.
    with socket.socket(socket.AF_UNIX) as stale:
        stale.bind(str(gateway.path))
    os.chmod(gateway.path, 0o600)
    replacement = RecoveryGateway(private_root)
    await replacement.start()
    try:
        assert json.loads(await request(replacement.path))["availability"] == "available"
    finally:
        await replacement.close()
    # Refuse a live listener even when it does not use the cooperative lock.
    with socket.socket(socket.AF_UNIX) as live:
        live.bind(str(gateway.path))
        os.chmod(gateway.path, 0o600)
        live.listen(1)
        foreign = RecoveryGateway(private_root)
        with pytest.raises(GatewayUnavailableError):
            await foreign.start()
        assert gateway.path.is_socket()
    gateway.path.unlink()
    gateway.path.symlink_to(private_root / "coordination.sqlite3")
    with pytest.raises(GatewayUnavailableError):
        await RecoveryGateway(private_root).start()
    assert gateway.path.is_symlink()


async def test_symlink_in_trusted_root_ancestor_fails_before_bind(private_root: Path):
    real = private_root / "real"
    real.mkdir(mode=0o755)
    nested = real / "nested"
    nested.mkdir(mode=0o755)
    copied = nested / "coordination.sqlite3"
    shutil.copyfile(private_root / "coordination.sqlite3", copied)
    copied.chmod(0o600)
    hop = private_root / "hop"
    hop.symlink_to(real, target_is_directory=True)
    gateway = RecoveryGateway(hop / "nested")
    with pytest.raises(GatewayUnavailableError, match="symlink component"):
        await gateway.start()
    assert not (nested / ".recovery-viewer").exists()


async def test_unsafe_ancestor_write_or_foreign_owner_refused_before_bind(
    private_root: Path, monkeypatch
):
    assert stat.S_ISVTX & Path("/var/tmp").stat().st_mode  # trusted root-owned sticky parent
    outer = private_root / "outer"
    outer.mkdir(mode=0o755)
    nested = outer / "nested"
    nested.mkdir(mode=0o755)
    copied = nested / "coordination.sqlite3"
    shutil.copyfile(private_root / "coordination.sqlite3", copied)
    copied.chmod(0o600)
    gateway = RecoveryGateway(nested)
    outer.chmod(0o777)
    try:
        with pytest.raises(GatewayUnavailableError, match="ancestor is replaceable"):
            await gateway.start()
        assert not gateway.directory.exists()
    finally:
        outer.chmod(0o755)
    original = Path.lstat

    def foreign_ancestor(path):
        info = original(path)
        if path == outer:
            values = list(info)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_ancestor)
    with pytest.raises(GatewayUnavailableError, match="ancestor has foreign ownership"):
        await gateway.start()
    assert not gateway.directory.exists()


async def test_prebind_mode_is_private_even_under_permissive_umask(private_root: Path):
    old = os.umask(0)
    try:
        gateway = RecoveryGateway(private_root)
        await gateway.start()
        try:
            assert stat.S_IMODE(gateway.path.stat().st_mode) == 0o600
            assert stat.S_IMODE(gateway.directory.stat().st_mode) == 0o700
            assert os.umask(0) == 0  # gateway restored the original mask
        finally:
            await gateway.close()
    finally:
        os.umask(old)


@pytest.mark.parametrize("foreign", ["database", "directory"])
async def test_foreign_owner_uid_fails_before_bind(private_root: Path, monkeypatch, foreign: str):
    gateway = RecoveryGateway(private_root)
    if foreign == "directory":
        gateway.directory.mkdir(mode=0o700)
    target = gateway.database if foreign == "database" else gateway.directory
    original = Path.lstat

    def foreign_uid(path):
        info = original(path)
        if path == target:
            values = list(info)
            values[4] += 1  # stat_result.st_uid, without requiring root/chown
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_uid)
    with pytest.raises(GatewayUnavailableError):
        await gateway.start()
    assert not gateway.path.exists()


async def test_unsafe_directory_root_permissions_db_and_wal_fail_closed(private_root: Path):
    db = private_root / "coordination.sqlite3"
    root_mode = stat.S_IMODE(private_root.stat().st_mode)
    os.chmod(private_root, 0o777)
    try:
        with pytest.raises(GatewayUnavailableError):
            await RecoveryGateway(private_root).start()
        assert not (private_root / ".recovery-viewer").exists()
    finally:
        os.chmod(private_root, root_mode)
    too_long = private_root / ("x" * 85)
    too_long.mkdir(mode=0o700)
    copied = too_long / "coordination.sqlite3"
    shutil.copyfile(db, copied)
    os.chmod(copied, 0o600)
    with pytest.raises(GatewayUnavailableError):
        await RecoveryGateway(too_long).start()
    assert not (too_long / ".recovery-viewer").exists()
    link = private_root.parent / f"{private_root.name}-link"
    link.symlink_to(private_root, target_is_directory=True)
    try:
        with pytest.raises(GatewayUnavailableError):
            await RecoveryGateway(link).start()
    finally:
        link.unlink()
    gateway = RecoveryGateway(private_root)
    gateway.directory.symlink_to(private_root, target_is_directory=True)
    with pytest.raises(GatewayUnavailableError):
        await gateway.start()
    assert gateway.directory.is_symlink()
    gateway.directory.unlink()
    gateway.directory.mkdir(mode=0o700)
    os.chmod(gateway.directory, 0o755)
    with pytest.raises(GatewayUnavailableError):
        await gateway.start()
    os.chmod(gateway.directory, 0o700)
    os.chmod(db, 0o644)
    with pytest.raises(GatewayUnavailableError):
        await gateway.start()
    os.chmod(db, 0o600)
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    baseline = {p.name for p in private_root.iterdir()}
    with pytest.raises(GatewayUnavailableError):
        await gateway.start()
    assert {p.name for p in private_root.iterdir()} == baseline
    assert not (private_root / "coordination.sqlite3-shm").exists()


def test_darwin_peer_uid_uses_kernel_dup_not_transport_claim(monkeypatch):
    import agent_comms.recovery_gateway as module

    class Peer:
        closed = False

        def getpeereid(self):
            return os.geteuid(), os.getegid()

        def close(self):
            self.closed = True

    class Transport:
        def __init__(self):
            self.peer = Peer()

        def dup(self):
            return self.peer

    monkeypatch.setattr(module.sys, "platform", "darwin")
    transport = Transport()
    assert module._peer_uid(transport) == os.geteuid()
    assert transport.peer.closed


async def test_unsupported_platform_fails_closed_before_directory_creation(
    private_root: Path, monkeypatch
):
    import agent_comms.recovery_gateway as module

    monkeypatch.setattr(module.sys, "platform", "win32")
    with pytest.raises(GatewayUnavailableError):
        await RecoveryGateway(private_root).start()
    assert not (private_root / ".recovery-viewer").exists()
