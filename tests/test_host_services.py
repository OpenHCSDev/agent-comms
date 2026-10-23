"""Restart success includes the connector's actual served read, not only its interpreter."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from connector_readiness import ConnectorReadiness
from agent_comms.connector import ConnectorVerb
from agent_comms.connector_store import ConnectorStore
from agent_comms.host_services import HostService, ServiceNotServingError
from agent_comms.host_services.connector import InstallServiceVerb, SystemdConnectorService
from agent_comms.installer.backup import StoreBackup
from agent_comms.launch_root import LaunchRoot
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.locked_store import DocumentChange


def test_the_connector_is_a_declared_service():
    assert SystemdConnectorService in HostService.members_with(HostService)


def test_a_private_home_does_not_discover_the_real_user_service(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not SystemdConnectorService.unit_file().exists()
    assert SystemdConnectorService.running() == ()


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
    store.update(DocumentChange.to)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    children: list[subprocess.Popen] = []
    project = Path(__file__).resolve().parents[1]
    readiness = ConnectorReadiness()
    environment = readiness.environment({**os.environ, "PYTHONPATH": str(project / "src")})
    command = (
        sys.executable, str(Path(sys.executable).parent / "agent-comms-connector"),
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
        def pid(cls) -> int:
            return next((process.pid for process in children if process.poll() is None), 0)

        def restart(self) -> None:
            previous = next(process for process in children if process.pid == self.pid())
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
    assert result.runtime == Path(sys.executable)
    assert result.line() == (f"Connector on port {service.port} for {service.launch.path} "
                             f"restarted onto {sys.executable}")
    assert service.result.round_trip in capsys.readouterr().err
    assert not service.store.read().access  # Its one short-lived proof grant was revoked.


def test_connector_unit_owns_exec_start_and_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "bus with spaces %and $value"
    command = ConnectorVerb.parse((
        "install-service", "--root", str(root), "--port", "18821",
        "--issuer", "https://connector.example.test",
    ))
    assert isinstance(command, InstallServiceVerb)
    service = SystemdConnectorService(
        launch=LaunchRoot.from_path(command.root), port=command.port,
    )
    text = service.unit_text(service.exec_arguments(command.issuer))
    expected = (
        f'ExecStart="{LaunchRuntime.current_pointer()}/bin/agent-comms-connector" '
        f'"serve" "--root" "{tmp_path}/bus with spaces %%and $$value" "--port" "18821" '
        '"--issuer" "https://connector.example.test"'
    )
    assert expected in text.splitlines()
    assert "Type=notify" in text.splitlines()
    assert "NotifyAccess=main" in text.splitlines()


def test_restart_refuses_a_connector_with_an_undecodable_store(connector_service):
    service = connector_service
    service.store.path.write_text('{"not_a_grant": true}')
    with pytest.raises(ServiceNotServingError, match="RecordProblems"):
        service.restart_onto(Path(sys.executable))
    with socket.socket() as refused:
        assert refused.connect_ex(("127.0.0.1", service.port)) != 0
