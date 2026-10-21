"""Restart success includes the connector's actual served read, not only its interpreter."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from connector_readiness import ConnectorReadiness
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
    readiness = ConnectorReadiness()
    environment = readiness.environment({**os.environ, "PYTHONPATH": str(project / "src")})
    command = (
        sys.executable, "-u", str(Path(sys.executable).parent / "agent-comms-connector"),
        "serve", "--root", str(copied), "--port", str(port),
    )

    def start() -> subprocess.Popen:
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        children.append(process)
        readiness.wait(process, port)
        return process

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
                assert process.wait(timeout=5) == 0  # SIGTERM stops the server cleanly.
            assert process.stdout is not None
            process.stdout.close()
        readiness.close()


def test_restart_proves_a_healthy_connector_on_its_installed_runtime(connector_service, capsys):
    service = connector_service
    result = service.restart_onto(Path(sys.executable))
    assert str(Path(sys.executable)) in result
    assert service.result.round_trip in capsys.readouterr().err
    assert not service.store.read().access  # Its one short-lived proof grant was revoked.


def test_systemd_restart_declares_readiness_before_restarting(comms, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(subprocess, "run", lambda command, check: commands.append(command))
    service = SystemdConnectorService(
        1, launch=LaunchRoot.from_path(comms.root), store=ConnectorStore.owned(),
    )
    service.restart()
    assert "Type=notify" in service.readiness_file.read_text()
    assert service.readiness_file.parent.name == f"{SystemdConnectorService.unit}.d"
    assert commands == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "restart", SystemdConnectorService.unit),
    ]


def test_restart_refuses_a_connector_with_an_undecodable_store(connector_service):
    service = connector_service
    service.store.path.write_text('{"not_a_grant": true}')
    with pytest.raises(ServiceNotServingError, match="RecordProblems"):
        service.restart_onto(Path(sys.executable))
    with socket.socket() as refused:
        assert refused.connect_ex(("127.0.0.1", service.port)) != 0
