"""Restart success includes the connector's actual served read, not only its interpreter."""

from __future__ import annotations

from agent_comms.repo import Checkout
import asyncio
import os
import pwd
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
    ActiveRouteFile,
    route_new_bus,
)
from agent_comms.bus import Bus
from agent_comms.child_process import (
    NoResidentManager,
    ResidentManager,
    SystemdUserManager,
    UnmanagedHomeError,
)
from agent_comms.comms import client_wire
from agent_comms.connector import ConnectorVerb, SystemdSite
from agent_comms.connector_store import ConnectorStore
from agent_comms.nominal.field_codec import Id128
from agent_comms.harness_install import CodexHarness
from agent_comms.host_services import (
    HostService,
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
from agent_comms.installer.logs import InstallUnit
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


@pytest.fixture
def reachable_manager(manager_socket, monkeypatch):
    """A real, owned socket tests admission without ever invoking a real manager."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(manager_socket))

    def reached(command, *arguments, **options):
        raise AssertionError(f"reached the user manager: {command}")

    monkeypatch.setattr(subprocess, "run", reached)
    monkeypatch.setattr(subprocess, "check_output", reached)
    return manager_socket


def test_only_the_users_own_home_has_the_user_manager(reachable_manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    manager = ResidentManager.current()
    assert manager == SystemdUserManager(reachable_manager)
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert manager.handoff_environment({})["XDG_RUNTIME_DIR"] == str(reachable_manager)

    def shown(argv, *, text, env):
        assert env["XDG_RUNTIME_DIR"] == str(reachable_manager)
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={reachable_manager}/bus"
        return "active"

    monkeypatch.setattr(subprocess, "check_output", shown)
    assert manager.activity("owned.service").serving()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ResidentManager.current() == NoResidentManager()


def test_an_explicit_invalid_runtime_never_falls_back_to_the_real_manager(
        manager_socket, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.chdir(manager_socket)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(manager_socket))
    assert ResidentManager.current() == SystemdUserManager(manager_socket.resolve())
    for directory in ("", ".", "./", f"../{manager_socket.name}",
                      str(tmp_path / "missing"), str(tmp_path)):
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        assert ResidentManager.current() == NoResidentManager()


def test_a_foreign_uid_cannot_admit_another_users_socket(manager_socket, monkeypatch):
    account = pwd.getpwuid(os.getuid())
    monkeypatch.setenv("HOME", account.pw_dir)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(manager_socket))
    monkeypatch.setattr(os, "getuid", lambda: account.pw_uid + 1)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: account)
    assert ResidentManager.current() == NoResidentManager()


INBOX = CodexInboxService("private-session")
CONNECTOR = SystemdConnectorService(launch=LaunchRoot.from_path(Path("/nonexistent")), port=1)
UNIT = InstallUnit(Id128.mint(), Path("/nonexistent"))


@pytest.mark.parametrize("verb", (
    INBOX.enable, INBOX.retire, INBOX.restore, INBOX.start, CONNECTOR.retire, CONNECTOR.start,
    lambda: HostService.stop_all((INBOX, CONNECTOR)), UNIT.stop,
    lambda: UNIT.start(Path("/nonexistent"), ("true",), "", wait=False),
), ids=("enable", "retire", "restore", "start", "connector-retire", "connector-start",
        "stop-all", "install-stop", "install-detached"))
def test_a_private_home_cannot_change_the_users_units(reachable_manager, tmp_path, monkeypatch,
                                                       verb):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(UnmanagedHomeError, match="not the user's own home|no user manager"):
        verb()


def test_an_enabled_start_without_its_promised_instance_is_not_success(
        tmp_path, monkeypatch, user_manager):
    monkeypatch.setenv("HOME", str(tmp_path))
    definition = SystemdConnectorService.unit_file(tmp_path)
    definition.parent.mkdir(parents=True, exist_ok=True)
    definition.write_text("[Unit]\n")
    wants = definition.parent / "default.target.wants"
    wants.mkdir()
    (wants / SystemdConnectorService.unit).touch()

    def output(argv, *, text, env):
        assert env["XDG_RUNTIME_DIR"] == str(user_manager.runtime_directory)
        return str(definition) if argv[4] == "FragmentPath" else "0"

    def start(argv, *, check, env):
        assert argv == ("systemctl", "--user", "start", SystemdConnectorService.unit)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "check_output", output)
    monkeypatch.setattr(subprocess, "run", start)
    with pytest.raises(ServiceNotServingError, match="started without one running instance"):
        HostService.start_enabled()


def test_a_private_home_does_not_discover_or_reload_the_real_user_service(
        reachable_manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ResidentManager.current().install(CodexInboxService, home=tmp_path)
    ResidentManager.current().install(SystemdConnectorService, home=tmp_path)
    (tmp_path / ".config/systemd/user/default.target.wants").mkdir()
    (tmp_path / ".config/systemd/user/default.target.wants" / SystemdConnectorService.unit).touch()
    HostService.start_enabled()
    assert SystemdConnectorService.running() == ()
    assert CodexInboxService.running() == ()
    assert HostService.instances() == ()
    assert not UNIT.running()


@pytest.mark.parametrize("enabled,foreign,started", (
    pytest.param((SystemdConnectorService.unit,), (), (SystemdConnectorService.unit,), id="ordinary"),
    pytest.param(
        ("codex-comms-inbox@a.service", "codex-comms-inbox@b.service", "codex-comms-inbox@.service",
         "codex-comms-inbox@foreign.service"),
        ("codex-comms-inbox@foreign.service",),
        ("codex-comms-inbox@a.service", "codex-comms-inbox@b.service"),
        id="template-instances"),
    pytest.param((), (), (), id="nothing-enabled"),
))
def test_start_enabled_starts_each_enabled_stopped_instance_its_home_defines(
        tmp_path, monkeypatch, connector_service, enabled, foreign, started):
    """Systemd enables a template's instances, never the template: an install starts every
    enabled instance this home defines. A disabled sibling (no link), the bare template
    link and a unit loaded from another home's definition stay stopped."""
    home = Path.home()
    pid = connector_service.pid()
    for service in (CodexInboxService, SystemdConnectorService):
        service.unit_file(home).parent.mkdir(parents=True, exist_ok=True)
        service.unit_file(home).write_text("[Unit]\n")
    wants = home / ".config/systemd/user/default.target.wants"
    wants.mkdir()
    for name in enabled:
        (wants / name).touch()
    elsewhere = tmp_path / "other-home" / CodexInboxService.unit
    starts: list[str] = []

    def control(command: tuple[str, ...], *, check: bool, env) -> subprocess.CompletedProcess[bytes]:
        assert check and command[:3] == ("systemctl", "--user", "start")
        starts.extend(command[3:])
        return subprocess.CompletedProcess(command, 0)

    def output(command: tuple[str, ...], *, text: bool, env) -> str:
        assert text
        if command[2] == "list-units":
            return "\n".join(name for name in starts if "@" in name)
        name = command[-1]
        field = command[4]
        if field == "MainPID":
            return str(pid if name in starts else 0)
        assert field == "FragmentPath"
        definition = (elsewhere if name in foreign else CodexInboxService.unit_file(home)
                      if "@" in name else SystemdConnectorService.unit_file(home))
        return f"{definition}\n"

    monkeypatch.setattr(subprocess, "run", control)
    monkeypatch.setattr(subprocess, "check_output", output)
    instances = HostService.start_enabled()
    assert sorted(starts) == sorted(started)
    assert sorted(instance.unit_name() for instance in instances) == sorted(started)


def test_codex_inbox_template_publication_reloads_each_launcher_home_change(tmp_path, monkeypatch, user_manager):
    monkeypatch.setenv("HOME", str(tmp_path / "ambient-home"))
    home = tmp_path / "harness-home"
    template = CodexInboxService.unit_file(home)
    runtime = LaunchRuntime.current_pointer()
    previous = runtime
    loaded: list[str] = []

    def run(command, *, check, env):
        assert check is True
        assert command == ("systemctl", "--user", "daemon-reload")
        loaded.append(template.read_text())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)

    assert CodexHarness(home).configure() == (template,)
    assert not CodexInboxService.unit_file(Path.home()).exists()
    assert len(loaded) == 1
    assert str(runtime / "bin/agent-comms-codex-bridge") in loaded[0]
    assert '"watch" --session "%i"' in loaded[0]
    assert "Type=simple" in loaded[0]
    assert "NotifyAccess=main" not in loaded[0]

    monkeypatch.setenv("HOME", str(tmp_path / "relocated-home"))
    runtime = LaunchRuntime.current_pointer()
    ResidentManager.current().install(CodexInboxService, home=home)
    assert len(loaded) == 2
    assert str(runtime / "bin/agent-comms-codex-bridge") in loaded[1]
    assert str(previous) not in loaded[1]
    assert loaded[1] == template.read_text()


@pytest.fixture
def connector_service(comms, tmp_path, monkeypatch, pinned_native, user_manager):
    """A real supervisor on a copied bus; only systemd's process control is replaced."""
    for name in ("claude-ai", "worker"):
        comms.registry.declare(Thread(name, frozenset(), Checkout(comms.root)))
    copied = tmp_path / "copied-bus"
    StoreBackup.copy_tree(comms.root, copied)
    with client_wire(copied) as copied_comms:
        copied_comms.registry.store.prepare_native_root()
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

    service = SystemdConnectorService(launch=LaunchRoot.from_path(copied), port=port)
    definition = service.unit_file(home)
    definition.parent.mkdir(parents=True, exist_ok=True)
    definition.write_text(service.unit_text(service.exec_arguments("")))
    original_run = subprocess.run
    original_output = subprocess.check_output

    def control(argv, *, check, env=None, **options):
        if argv[:2] != ("systemctl", "--user"):
            return original_run(argv, check=check, env=env, **options)
        assert env["XDG_RUNTIME_DIR"] == str(user_manager.runtime_directory)
        assert argv[3:] == (service.unit_name(),)
        if argv[2] == "stop":
            for process in children:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
        else:
            assert argv[2] == "start"
            if not any(process.poll() is None for process in children):
                start()
        return subprocess.CompletedProcess(argv, 0)

    def output(argv, *, text, env=None, **options):
        if argv[:2] != ("systemctl", "--user"):
            return original_output(argv, text=text, env=env, **options)
        assert env["XDG_RUNTIME_DIR"] == str(user_manager.runtime_directory)
        assert argv[-1] == service.unit_name()
        field = argv[4]
        if field == "FragmentPath":
            return str(definition)
        pid = next((process.pid for process in children if process.poll() is None), 0)
        if field == "MainPID":
            return str(pid)
        assert field == "ActiveState"
        return "active" if pid else "inactive"

    monkeypatch.setattr(subprocess, "run", control)
    monkeypatch.setattr(subprocess, "check_output", output)
    try:
        start()
        yield SystemdConnectorService.running()[0]
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


@pytest.mark.parametrize("stop_interrupted", [False, True], ids=["owner-start", "stop-call"])
def test_failure_restores_stopped_services_once(
    connector_service: SystemdConnectorService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    stop_interrupted: bool,
) -> None:
    """A failure after the stop, or one raised inside the stop call once systemd has stopped
    the unit (the SIGTERM an installer inside that unit receives), starts it again once."""
    service = connector_service
    unit = service.unit_file(Path.home())
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.touch()
    starts: list[str] = []
    failure = OSError("owner start failed")
    original_run = subprocess.run

    def control(
        command: tuple[str, ...], *, check: bool, env=None, **options,
    ) -> subprocess.CompletedProcess[bytes]:
        result = original_run(command, check=check, env=env, **options)
        if command[:3] == ("systemctl", "--user", "stop") and stop_interrupted:
            raise failure
        if command == ("systemctl", "--user", "start", service.unit_name()):
            starts.append(service.unit_name())
        return result

    monkeypatch.setattr(subprocess, "run", control)

    with InstallCommands() as commands:
        runtime = LaunchRuntime.running()
        installed = Stack(tmp_path, commands, Path.home(), runtime)
        staged = Stack(tmp_path, commands, Path.home(), runtime)
        with (
            pytest.raises(OSError) as raised,
            staged.services_stopped(installed) as stopped,
        ):
            descriptions = tuple(item.described() for item in stopped.services)
            assert descriptions == (service.described(),)
            assert service.pid() == 0
            raise failure
        assert raised.value is failure
        assert starts == [service.unit_name()]
        assert service.pid() != 0
        service.serves()


def test_connector_refuses_a_different_published_bus(connector_service, tmp_path):
    service = connector_service
    other = tmp_path / "other-published-bus"
    StoreBackup.copy_tree(service.launch.path, other)
    route = ActiveRoute(other, RecordedBusIdentity.capture(Bus.path_in(other)))
    path = active_route_path()
    ActiveRouteFile(path).publish(
        route, lambda current, publish, directory: current.replace(path, publish, directory))

    with pytest.raises(ServiceNotServingError, match="published route selects") as refused:
        service.serves()
    assert str(Bus.path_in(service.launch.path)) in str(refused.value)
    assert str(Bus.path_in(other)) in str(refused.value)


def test_round_trip_refuses_a_different_requested_bus(connector_service, tmp_path, monkeypatch):
    service = connector_service
    other = tmp_path / "other-requested-bus"
    StoreBackup.copy_tree(service.launch.path, other)
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
                worktree=Checkout(service.launch.path),
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


def test_services_of_every_member_stop_in_one_call(tmp_path, monkeypatch, user_manager):
    stops: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], *, check: bool, env) -> subprocess.CompletedProcess[bytes]:
        stops.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    inboxes = (CodexInboxService("a"), CodexInboxService("b"))
    connector = SystemdConnectorService(launch=LaunchRoot.from_path(tmp_path), port=1)
    HostService.stop_all((*inboxes, connector))
    assert stops == [(
        "systemctl", "--user", "stop",
        "codex-comms-inbox@a.service", "codex-comms-inbox@b.service", connector.unit_name(),
    )]
