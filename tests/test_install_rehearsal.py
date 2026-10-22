"""The install launch path, run the way an owner runs it, before any merge lands.

0765a002c broke every ``agent-comms install`` without ``--repos``, and install 21
refused because the target's owner probe asked the outgoing runtime for a method it no
longer had (fixed in #2419); every required check passed both, because nothing ran the
installer. This rehearses a real bus through the CLI under test, with the merge base's
build installed as the outgoing runtime: launch, target tree, staged stack build,
carried practice copy and the outgoing runtime's owner probe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from traceback import FrameSummary, extract_stack
from typing import ClassVar, NoReturn

import pytest

import agent_comms
from agent_comms.cli_bridges.monitor import InspectSession
from agent_comms.cli_bridges.shared import ResumedBridge
from agent_comms.cli_commands import HistoryCliCommand, InstallCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.harness_install import replace_link
from agent_comms.installer.launch import InstallReport, InstallRun
from agent_comms.installer.outcome import CrashedInstall, FailedInstall
from agent_comms.installer.reporting import CarriedWriter
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.installer.upgrade import (
    CarriedRehearsal,
    RehearsedUpgrade,
    Stack,
    Upgrade,
    upgrade,
)
from agent_comms.launch_runtime import LaunchRuntime
from conftest import declare_test_bus, publish_initial, short_home
from test_install_session_bridge import session_bridge

REPOSITORY = Path(__file__).resolve().parents[1]


def git(*arguments: str) -> str:
    return subprocess.run(["git", "-C", REPOSITORY, *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


@dataclass(frozen=True)
class InstalledHome:
    environment: dict[str, str]
    outgoing: LaunchRuntime


@pytest.fixture(scope="module")
def installed_home(tmp_path_factory: pytest.TempPathFactory) -> InstalledHome:
    """A home holding what an installed machine has: the installer's uv, and as the
    installed runtime a build of the merge base this change lands on. Both cases share
    it, so the second reuses the staged runtime the first built, as a second install
    would. Package caches stay shared."""
    home = tmp_path_factory.mktemp("installed-home")
    uv = home / ".local/state/agent-comms/upgrade-bus/uv"
    uv.parent.mkdir(parents=True)
    uv_program = shutil.which("uv")
    assert uv_program is not None, "The installed-runtime fixture requires uv"
    uv.symlink_to(uv_program)
    caches = subprocess.run([uv, "cache", "dir"], check=True, capture_output=True, text=True)
    environment = {**os.environ, "HOME": str(home), "UV_CACHE_DIR": caches.stdout.strip(),
                   "npm_config_cache": str(Path.home() / ".npm")}
    environment.pop("PYTHONPATH", None)
    base = home / "outgoing-source"
    archive = subprocess.run(
        ["git", "-C", REPOSITORY, "archive", git("merge-base", "HEAD", "origin/main")],
        check=True, capture_output=True).stdout
    with tarfile.open(fileobj=BytesIO(archive)) as tree:
        tree.extractall(base, filter="data")
    runtime = home / ".local/share/agent-comms/runtimes/outgoing"
    subprocess.run([uv, "venv", "-q", "--python", sys.executable, runtime],
                   check=True, env=environment)
    subprocess.run([uv, "pip", "install", "-q", "--python", runtime / "bin/python", base],
                   check=True, env=environment)
    (home / ".local/share/agent-comms/current").symlink_to(runtime, target_is_directory=True)
    return InstalledHome(environment, LaunchRuntime(runtime / "bin/python"))


@pytest.mark.timeout(280)
@pytest.mark.parametrize("repos", ((), ("--repos",)), ids=("kept-repos", "given-repos"))
def test_install_rehearses_preserving_existing_data_and_publishing_its_result(
        private_comms: Comms, installed_home: InstalledHome, tmp_path: Path,
        repos: tuple[str, ...]) -> None:
    comms = private_comms
    declare_test_bus(comms, channels=("comms",), threads=(
        comms.registry.new_thread(name="worker", tags=frozenset(), worktree=str(tmp_path)),))
    publish_initial(comms, "worker", "#all", "One message before the install")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", REPOSITORY, checkout], check=True)
    before = HistoryCliCommand().apply(comms).messages
    worker = comms.registry.require("worker")
    given = (*repos, str(checkout)) if repos else ()
    installed = subprocess.run(
        [Path(sys.executable).with_name("agent-comms"), "--root", comms.root, "install",
         "--rehearse", "--checkout", checkout, "--to", "HEAD", "--project", checkout, *given],
        cwd=checkout,
        env={**installed_home.environment, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        capture_output=True, text=True, timeout=270)
    assert installed.returncode == 0, installed.stderr
    result = FieldCodec.decode(Upgrade, FieldCodec.parse(installed.stdout))
    assert isinstance(result, RehearsedUpgrade) and isinstance(result.rehearsal, CarriedRehearsal)
    after = HistoryCliCommand().apply(comms).messages
    assert comms.registry.require("worker") == worker
    original = {message.reference: message for message in before}
    retained = {message.reference: message for message in after}
    assert {reference: retained[reference] for reference in original} == original
    assert len(after) == len(before) + 2
    reports = tuple(reversed(HistoryCliCommand(
        target=f"#{InstallReport.channel}").apply(comms).messages))
    assert len(reports) == 2
    assert all(message.sender == InstallReport.sender for message in reports)
    assert reports[0].body.splitlines()[0] == "Install starting"
    assert reports[1].body.splitlines()[0] == "Install finished"


class PublicationFault(BaseException):
    """The injected failure retains the actual source caller, not a copied line number."""

    def __init__(self, site: FrameSummary):
        super().__init__("The target was published before this failure")
        self.site = site


class PublicationRuntimeError(PublicationFault, RuntimeError):
    pass


class PublicationInterrupt(PublicationFault, KeyboardInterrupt):
    pass


class PublicationFailingRun(InstallRun):
    """Inject only the requested final-step failure; the entire install is real."""

    error_kind: ClassVar[type[PublicationFault]] = PublicationRuntimeError

    def harnesses(self) -> NoReturn:
        assert RuntimeResolution.at(LaunchRuntime.installed().prefix).core_commit == self.target
        raise self.error_kind(extract_stack(limit=2)[0])


class PublicationInterruptedRun(PublicationFailingRun):
    error_kind = PublicationInterrupt


@pytest.fixture
def publication_home(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
                     tmp_path: Path) -> Iterator[tuple[Path, LaunchRuntime]]:
    """The hosted installed-runtime job supplies the build; source workers never build one."""
    candidate = LaunchRuntime.running()
    if not Path(agent_comms.__file__).resolve().is_relative_to(candidate.prefix.resolve()):
        pytest.skip("Full publication proof requires the hosted built-runtime job")
    installed: InstalledHome = request.getfixturevalue("installed_home")
    for name, value in installed.environment.items():
        monkeypatch.setenv(name, value)
    replace_link(LaunchRuntime.current_pointer(), installed.outgoing.prefix)
    # An isolated home must never restart the test runner's real user services.
    binary = tmp_path / "host-programs"
    binary.mkdir()
    systemctl = binary / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "--user" ] && [ "$2" = "show" ]; then\n'
        "  printf '0\\n'\n"
        'else\n'
        '  exit 1\n'
        'fi\n'
    )
    systemctl.chmod(0o700)
    monkeypatch.setenv("PATH", os.pathsep.join((str(binary), os.environ["PATH"])))
    # Only the bridge socket needs a short locator; CLI storage keeps its trusted home.
    with short_home(Path(installed.environment["HOME"])) as home:
        monkeypatch.setenv("HOME", str(home))
        yield home, candidate


@pytest.mark.timeout(280)
@pytest.mark.parametrize("run_type,completion,terminal", (
    pytest.param(InstallRun, nullcontext(), "Install finished", id="finished"),
    pytest.param(
        PublicationFailingRun, pytest.raises(RuntimeError), "Install failed", id="crashed"),
    pytest.param(
        PublicationInterruptedRun, pytest.raises(KeyboardInterrupt), "Install failed",
        id="interrupted",
    ),
))
def test_live_upgrade_family_after_real_publication(
        publication_home: tuple[Path, LaunchRuntime], tmp_path: Path,
        run_type: type[InstallRun], completion: AbstractContextManager[object],
        terminal: str) -> None:
    home, candidate = publication_home
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", REPOSITORY, checkout], check=True)
    target = git("rev-parse", "HEAD")
    subprocess.run(["git", "-C", checkout, "checkout", "--detach", "-q",
                    git("rev-parse", "HEAD^")], check=True)
    outgoing = Stack(checkout, home=home, runtime=LaunchRuntime.installed())
    program = (str(candidate.interpreter.with_name("agent-comms-claude-bridge")),)
    with session_bridge(tmp_path, home, program, session=tmp_path.name) as copied:
        comms = Comms(tmp_path / "bus")
        declare_test_bus(comms, channels=(InstallReport.channel,))
        publish_initial(comms, copied.binding.bus, "#all", "Retained before the upgrade")
        before = HistoryCliCommand().apply(comms).messages
        request = run_type.start(InstallCliCommand(checkout=checkout, to=target, project=checkout),
                                 comms.root)
        report = InstallReport(comms.root, outgoing)
        with completion as caught:
            upgrade(request, report)
        assert isinstance(report.writer, CarriedWriter)
        if isinstance(report.outcome, FailedInstall):
            failure = report.outcome.failure
            assert isinstance(failure, CrashedInstall)
            error = failure.error
            assert isinstance(error, PublicationFault)
            assert isinstance(caught, pytest.ExceptionInfo)
            assert caught.value is error
            assert (failure.frame.filename, failure.frame.lineno) == (
                str(report.source.module_file(upgrade.__module__)), error.site.lineno,
            )
        assert RuntimeResolution.at(LaunchRuntime.installed().prefix).core_commit == target
        assert LaunchRuntime.installed().prefix != outgoing.runtime.prefix
        resumed = InspectSession().information(copied.claim)
        assert resumed.process == copied.observed.process
        assert resumed.process.pid == copied.child.pid
        assert resumed.runtime.prefix == LaunchRuntime.installed().prefix.resolve()
        assert copied.child.poll() is None, copied.log.read_text()
        reports = tuple(reversed(HistoryCliCommand(target=report.target).apply(comms).messages))
        assert len(reports) == 2
        assert reports[0].body.splitlines()[0] == "Install starting"
        assert reports[1].body.splitlines()[0].startswith(terminal)
        assert all(line in reports[1].body for line in report.outcome.lines())
        bridge_line = ResumedBridge(
            copied.claim.thread, copied.observed.runtime, resumed.runtime).line()
        assert reports[1].body.splitlines().count(bridge_line) == 1
        retained = {
            message.reference: message for message in HistoryCliCommand().apply(comms).messages
        }
        assert all(retained[message.reference] == message for message in before)
        assert not request.scratch.exists()
