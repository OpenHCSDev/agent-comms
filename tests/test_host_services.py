"""Restart success includes the connector's actual served read, not only its interpreter."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from agent_comms.active_route import (
    ActiveRoute,
    RouteFile,
    active_route_path,
    route_directory,
    route_new_bus,
)
from agent_comms.bus import Bus
from agent_comms.comms import client_wire
from agent_comms.connector import ConnectorVerb, SystemdSite
from agent_comms.connector_store import ConnectorStore
from agent_comms.harness_install import CodexHarness
from agent_comms.host_services import (
    HostService,
    RestartedService,
    ServiceNotServingError,
)
from agent_comms.host_services.codex_inbox import CodexInboxService
from agent_comms.host_services.connector import (
    InstallServiceVerb,
    RoundTripVerb,
    SystemdConnectorService,
)
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.owner_starts import OwnerStart
from agent_comms.installer.upgrade import Stack
from agent_comms.launch_root import LaunchRoot
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.locked_store import DocumentChange
from agent_comms.recorded_bus import RecordedBusIdentity
from agent_comms.threads import Thread
from connector_readiness import ConnectorReadiness


def test_site_notifies_only_after_successful_start(monkeypatch):
    address = f"@p404-ready-{os.getpid()}"
    monkeypatch.setenv(SystemdSite.notify_variable, address)

    async def serve():
        app = web.Application()

        async def read(request):
            return web.Response(text="serving")

        app.router.add_get("/", read)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            with (
                socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as supervisor,
                socket.socket() as occupied,
            ):
                supervisor.bind("\0" + address[1:])
                supervisor.settimeout(0.02)
                site = SystemdSite(runner, "127.0.0.1", 0)
                with pytest.raises(TimeoutError):
                    supervisor.recv(64)
                occupied.bind(("127.0.0.1", 0))
                occupied.listen()
                blocked = SystemdSite(runner, "127.0.0.1", occupied.getsockname()[1])
                with pytest.raises(OSError):
                    await blocked.start()
                with pytest.raises(TimeoutError):
                    supervisor.recv(64)
                await site.start()
                assert supervisor.recv(64) == b"READY=1"
                port = runner.addresses[0][1]
                async with (
                    ClientSession() as client,
                    client.get(f"http://127.0.0.1:{port}/") as reply,
                ):
                    assert await reply.text() == "serving"
                await site.stop()
                with pytest.raises(TimeoutError):
                    supervisor.recv(64)
        finally:
            await runner.cleanup()

    asyncio.run(serve())


def test_the_connector_is_a_declared_service():
    assert SystemdConnectorService in HostService.members_with(HostService)


def test_a_private_home_does_not_discover_the_real_user_service(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not SystemdConnectorService.unit_file(tmp_path).exists()
    assert SystemdConnectorService.running() == ()


def test_codex_inbox_template_publication_reloads_each_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "ambient-home"))
    home = tmp_path / "harness-home"
    template = CodexInboxService.unit_file(home)
    runtime = tmp_path / "old-runtime"
    loaded: list[str] = []

    def run(command, *, check):
        assert check is True
        assert command == ("systemctl", "--user", "daemon-reload")
        loaded.append(template.read_text())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(LaunchRuntime, "current_pointer", classmethod(lambda cls: runtime))

    assert CodexHarness(home).configure() == (template,)
    assert not CodexInboxService.unit_file(Path.home()).exists()
    assert len(loaded) == 1
    assert str(runtime / "bin/agent-comms-codex-bridge") in loaded[0]
    assert '"watch" --session "%i"' in loaded[0]
    assert "Type=simple" in loaded[0]
    assert "NotifyAccess=main" not in loaded[0]

    runtime = tmp_path / "new-runtime"
    CodexInboxService.install(home=home)
    assert len(loaded) == 2
    assert str(runtime / "bin/agent-comms-codex-bridge") in loaded[1]
    assert "old-runtime" not in loaded[1]
    assert loaded[1] == template.read_text()


@pytest.fixture
def connector_service(comms, tmp_path, monkeypatch, pinned_native):
    """A real supervisor on a copied bus; only systemd's process control is replaced."""
    for name in ("claude-ai", "worker"):
        comms.registry.declare(Thread(name, frozenset(), str(comms.root)))
    copied = tmp_path / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied)
    with client_wire(copied) as copied_comms:
        copied_comms.registry.prepare_native_root()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    route_new_bus(
        ActiveRoute(copied, RecordedBusIdentity.capture(Bus.path_in(copied))), active_route_path()
    )
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
        sys.executable,
        str(Path(sys.executable).parent / "agent-comms-connector"),
        "serve",
        "--root",
        str(copied),
        "--port",
        str(port),
    )

    def start() -> subprocess.Popen:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        children.append(process)
        readiness.wait(process, port)
        return process

    class ManagedConnector(SystemdConnectorService, declared_name=None):
        @classmethod
        def _pid_for(cls, name: str) -> int:
            return next((process.pid for process in children if process.poll() is None), 0)

        @classmethod
        def stop_all(cls, instances: tuple[SystemdConnectorService, ...]) -> None:
            for instance in instances:
                previous = next(process for process in children if process.pid == instance.pid())
                previous.terminate()
                previous.wait(timeout=5)

        def start(self) -> None:
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
    type(service).stop_all((service,))
    result = service.start_onto(Path(sys.executable))
    service.serves()  # ServingProbe's proof, run here by the root's own runtime.
    assert result.runtime == Path(sys.executable)
    assert result.line() == (
        f"Connector on port {service.port} for {service.launch.path} "
        f"restarted onto {sys.executable}"
    )
    assert service.result.round_trip in capsys.readouterr().err
    assert not service.store.read().access  # Its one short-lived proof grant was revoked.


def test_owner_start_failure_restores_stopped_services_once(
    connector_service: SystemdConnectorService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = connector_service
    unit = service.unit_file(Path.home())
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.touch()
    starts: list[str] = []
    restores: list[Stack] = []
    failure = OSError("owner start failed")
    original_run = subprocess.run
    original_output = subprocess.check_output

    def control(
        command: tuple[str, ...], *, check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        if command[:3] == ("systemctl", "--user", "stop"):
            assert command[3:] == (service.unit_name(),)
            type(service).stop_all((service,))
        elif command == ("systemctl", "--user", "start", service.unit_name()):
            starts.append(service.unit_name())
            service.start()
        else:
            return original_run(command, check=check)
        return subprocess.CompletedProcess(command, 0)

    def output(command: tuple[str, ...], *, text: bool) -> str:
        assert text
        if command == (
            "systemctl", "--user", "show", "-p", "FragmentPath", "--value", service.unit_name(),
        ):
            return f"{unit}\n"
        if command == (
            "systemctl", "--user", "show", "-p", "MainPID", "--value", service.unit_name(),
        ):
            return f"{service.pid()}\n"
        return original_output(command, text=True)

    monkeypatch.setattr(subprocess, "run", control)
    monkeypatch.setattr(subprocess, "check_output", output)

    class OwnerStartFailure(Stack):
        def start(self, root: Path, names: list[str]) -> tuple[OwnerStart, ...]:
            raise failure

    class RestoreStack(Stack):
        def start_services(
            self, stopped: tuple[HostService, ...],
        ) -> tuple[RestartedService, ...]:
            restores.append(self)
            return super().start_services(stopped)

    with InstallCommands() as commands:
        runtime = LaunchRuntime.running()
        installed = RestoreStack(tmp_path, commands, Path.home(), runtime)
        staged = OwnerStartFailure(tmp_path, commands, Path.home(), runtime)
        with (
            pytest.raises(OSError) as raised,
            staged.services_stopped(installed) as stopped,
        ):
            descriptions = tuple(item.described() for item in stopped.services)
            assert descriptions == (service.described(),)
            assert service.pid() == 0
            staged.start(service.launch.path, ["worker"])
        assert raised.value is failure
        assert restores == [installed]
        assert starts == [service.unit_name()]
        assert service.pid() != 0
        service.serves()


def test_connector_refuses_a_different_published_bus(connector_service, tmp_path):
    service = connector_service
    other = tmp_path / "other-published-bus"
    StoreBackup.copy_tree(service.launch.path, other)
    route = ActiveRoute(other, RecordedBusIdentity.capture(Bus.path_in(other)))
    path = active_route_path()
    with route_directory(path) as directory:
        RouteFile.read(path).replace(route, path, directory)

    with pytest.raises(ServiceNotServingError, match="published route selects") as refused:
        service.serves()
    assert str(Bus.path_in(service.launch.path)) in str(refused.value)
    assert str(Bus.path_in(other)) in str(refused.value)


def test_round_trip_refuses_a_different_requested_bus(connector_service, tmp_path, monkeypatch):
    service = connector_service
    other = tmp_path / "other-requested-bus"
    StoreBackup.copy_tree(service.launch.path, other)
    monkeypatch.setattr(
        SystemdConnectorService, "running", classmethod(lambda cls: (service,))
    )

    refused = RoundTripVerb(root=other, port=service.port).apply(service.store)
    assert isinstance(refused, str)
    assert str(Bus.path_in(service.launch.path)) in refused
    assert str(Bus.path_in(other)) in refused


def test_connector_accepts_bus_writes_during_its_proof(connector_service, monkeypatch):
    service = connector_service
    open_response = urllib.request.urlopen

    @contextmanager
    def read_then_write(*args, **kwargs):
        with open_response(*args, **kwargs) as response:
            yield response
        with client_wire(service.launch.path) as comms:
            comms.channels.create_tag("proof-race")
            comms.registry.declare(Thread(
                name="proof-writer", tags=frozenset({"proof-race"}),
                worktree=str(service.launch.path),
            ))

    monkeypatch.setattr(urllib.request, "urlopen", read_then_write)
    service.serves()
    with client_wire(service.launch.path) as comms:
        assert comms.registry.require("proof-writer").tags == frozenset({"proof-race"})


def test_connector_unit_owns_exec_start_and_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "bus with spaces %and $value"
    command = ConnectorVerb.parse(
        (
            "install-service",
            "--root",
            str(root),
            "--port",
            "18821",
            "--issuer",
            "https://connector.example.test",
        )
    )
    assert isinstance(command, InstallServiceVerb)
    service = SystemdConnectorService(
        launch=LaunchRoot.from_path(command.root),
        port=command.port,
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
    type(service).stop_all((service,))
    service.store.path.write_text('{"not_a_grant": true}')
    with pytest.raises(ServiceNotServingError, match="RecordProblems"):
        service.start_onto(Path(sys.executable))
        service.serves()
    with socket.socket() as refused:
        assert refused.connect_ex(("127.0.0.1", service.port)) != 0
