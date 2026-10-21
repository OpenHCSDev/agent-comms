"""Restart success includes the connector's actual served read, not only its interpreter."""
from __future__ import annotations

import os
import selectors
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_comms.connector_store import ConnectorStore
from agent_comms.host_services import HostService, ServiceNotServingError
from agent_comms.host_services.connector import SystemdConnectorService
from agent_comms.installer.backup import StoreBackup
from agent_comms.launch_root import LaunchRoot


def test_the_connector_is_a_declared_service():
    assert SystemdConnectorService in HostService.members_with(HostService)


@pytest.fixture
def connector_service(comms, tmp_path, monkeypatch):
    """A real supervisor on a copied bus; only systemd's process control is replaced."""
    for name in ("claude-ai", "worker"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(comms.root)))
    copied = tmp_path / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    store = ConnectorStore.owned()
    store.update(lambda grants: grants)
    launch = LaunchRoot.from_path(copied)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    children: list[subprocess.Popen] = []
    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(project / "src")}
    command = (
        sys.executable, "-u", str(Path(sys.executable).parent / "agent-comms-connector"),
        "serve", "--root", str(copied), "--port", str(port),
    )

    def start() -> subprocess.Popen:
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        children.append(process)
        assert process.stdout is not None
        output = bytearray()
        deadline = time.monotonic() + 15
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not ready.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    process.wait(timeout=5)
                    raise ServiceNotServingError(
                        f"Connector exited before binding: {output.decode(errors='replace')}"
                    )
                output.extend(chunk)
                if b"Running on " in output:
                    return process
        raise ServiceNotServingError(f"Connector did not bind: {output.decode(errors='replace')}")

    class ManagedConnector(SystemdConnectorService, declared_name=None):
        @classmethod
        def running(cls):
            return tuple(
                cls(process.pid, launch=launch, store=store, port=port)
                for process in children if process.poll() is None
            )

        def restart(self) -> None:
            previous = next(process for process in children if process.pid == self.pid)
            previous.terminate()
            previous.wait(timeout=5)
            start()

    try:
        start()
        yield ManagedConnector.running()[0]
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            assert process.stdout is not None
            process.stdout.close()


def test_restart_proves_a_healthy_connector_on_its_installed_runtime(connector_service, capsys):
    service = connector_service
    result = service.restart_onto(Path(sys.executable))
    assert str(Path(sys.executable)) in result
    assert service.result.round_trip in capsys.readouterr().err
    assert not service.store.read().access  # Its one short-lived proof grant was revoked.


def test_restart_refuses_a_connector_with_an_undecodable_store(connector_service):
    service = connector_service
    service.store.path.write_text('{"not_a_grant": true}')
    with pytest.raises(ServiceNotServingError, match="RecordProblems"):
        service.restart_onto(Path(sys.executable))
    with socket.socket() as refused:
        assert refused.connect_ex(("127.0.0.1", service.port)) != 0
