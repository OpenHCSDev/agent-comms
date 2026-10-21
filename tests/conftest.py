"""Shared fixtures: an isolated Comms wire per test."""
from agent_comms.native_package import pinned_location, pinned_native_package

import asyncio
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from xdist import is_xdist_worker
from source_programs import SourcePrograms
from source_revision_fixture import SourceRepository

from agent_comms.bus_publication import AgentOrigin
from agent_comms.bus import Bus
from agent_comms.comms import Comms
from agent_comms.message_bus import MessageBus
from agent_comms.native_tool_transport import ToolListener
from agent_comms.publisher import ParticipantPublisher, Publisher
from agent_comms.registration import Registration
from agent_comms.messages import ConversationRole, InfoKind, Message
from agent_comms.thread_text import ForkedParent, GivenThreadText


class TranscriptRecords:
    """Fixed test time, projected through each record's declared constructor."""

    def __init__(self):
        from agent_comms.field_codec import RecordedTimestamp

        self.timestamp = RecordedTimestamp("2026-10-01T00:00:00.000Z")

    def event(self, declaration, *args, **facts):
        return declaration(*args, timestamp=self.timestamp.at.timestamp(), **facts)

    def entry(self, declaration, **facts):
        return declaration(timestamp=self.timestamp, **facts)

    def message(self, payload, **coordinates):
        from agent_comms.native_entries import MessageEntry
        from agent_comms.pi_payloads import PiMessage

        entry = self.entry(MessageEntry, message=PiMessage.from_wire(payload), **coordinates)
        return {**entry.to_wire(), "message": payload}

    def session(self, **coordinates):
        from agent_comms.native_entries import SessionEntry

        return self.entry(SessionEntry, **coordinates)


transcript_records = TranscriptRecords()


@pytest.fixture
def source_repository(tmp_path):
    return SourceRepository.create(tmp_path / 'source')


@pytest.fixture
def source_programs(tmp_path):
    return SourcePrograms.create(tmp_path)


@pytest.fixture
def bridge_home(tmp_path, monkeypatch):
    """Short per-worker socket addresses; all state stays in persistent test scratch."""
    destination = tmp_path / 'home'
    destination.mkdir()
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    home = Path(f'.bridge-home-{uuid.uuid4().hex[:8]}')
    home.symlink_to(destination, target_is_directory=True)
    try:
        yield home
    finally:
        home.unlink()


_MANAGED_RUNTIME_ENVIRONMENT = frozenset({
    "AGENT_COMMS_MANAGED",
    "AGENT_COMMS_PROJECT_REQUEST",
    "AGENT_COMMS_PROJECT_SOCKET",
    "AGENT_COMMS_ROOT",
    "AGENT_COMMS_THREAD",
    "PI_PARENT_ID",
    "PI_SESSION_FILE",
    "PI_SESSION_ID",
    "PI_TASK",
    "PI_WORKTREE",
})


async def await_event_or_task(event: Awaitable[object], task: asyncio.Task) -> object:
    """A producer failure ends its event wait with the original exception."""
    observed = asyncio.ensure_future(event)
    try:
        done, _ = await asyncio.wait((observed, task), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert observed.done(), 'Producer finished without the expected event'
        return observed.result()
    finally:
        observed.cancel()
        await asyncio.gather(observed, return_exceptions=True)


def reader(root: Path) -> Comms:
    """A Comms over an existing wire that makes no private write."""
    return Comms(root, Publisher)


def responder(root: Path, registry: Registration) -> MessageBus[ParticipantPublisher]:
    """The selected owner publishes replies and releases its attempt's claims."""
    return MessageBus(root / "bus.jsonl", registry, ParticipantPublisher)


def publish_initial(comms, sender, target, body, type=InfoKind, *, role=ConversationRole()):
    """Publish through the ordinary owner without submitting to a native agent."""
    type.require_sendable()
    return comms.bus.publisher.publish_ordinary(
        Message(sender=sender, target=target, body=body, type=type, role=role),
        origin=AgentOrigin(),
    )

def pytest_sessionstart(session):
    """The controller prepares this checkout's pinned inputs before any worker runs.

    Its pinned submodules are populated as every tree of ours is (Checkout), and
    prepare-pi-native keys its build by the pin and reuses a verified one, so an unchanged
    pin costs nothing and a changed pin rebuilds without anyone remembering.
    Collection loads declarations only; it neither starts workers nor needs either.
    """
    if session.config.option.collectonly or is_xdist_worker(session):
        return
    from agent_comms.repo import Checkout

    root = Path(__file__).resolve().parents[1]
    Checkout(root).prepare()
    if not pinned_location().is_dir():
        subprocess.run([str(root / "stack/bin/prepare-pi-native"), "--python", sys.executable], check=True)


@pytest.fixture
async def tool_listener():
    """Every private test endpoint has one joined retirement owner."""
    async with AsyncExitStack() as resources:
        def create(directory, token):
            listener = ToolListener(directory, token)
            resources.push_async_callback(listener.close)
            return listener
        yield create


@pytest.fixture
def durable_home(tmp_path, monkeypatch):
    """A credential-free durable host never reads the developer's Pi account."""
    home = tmp_path / "home"
    config = home / ".pi" / "agent"
    config.mkdir(parents=True)
    (config / "auth.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def source_reads():
    from agent_comms.native_entries import NativeEvidenceScope

    with NativeEvidenceScope() as scope:
        yield scope


@pytest.fixture
def cursor_delivery(source_reads):
    from agent_comms.cursor_publication import CursorDelivery

    return CursorDelivery(source_reads=source_reads)


@pytest.fixture(scope="session")
def native_runtime_package():
    """The running interpreter's artifact, qualified by the real installer before tests."""
    from agent_comms.native_package import VerifiedNativePackage
    from agent_comms.unit_instructions import UnitInstructions

    package = VerifiedNativePackage.acquire(pinned_native_package())
    UnitInstructions.prepare()
    return package


@pytest.fixture(autouse=True)
def _managed_runtime_environment_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's live managed process is not a test wire or native child."""
    from agent_comms.launch_root import LaunchRoot

    with LaunchRoot.unstarted().selected():
        for name in _MANAGED_RUNTIME_ENVIRONMENT:
            monkeypatch.delenv(name, raising=False)
        yield


@pytest.fixture
def scratch_root(tmp_path_factory):
    from agent_comms.storage_rules import StorageRule

    base = tmp_path_factory.getbasetemp()
    scratch = StorageRule().scratch_base.resolve()
    if not base.resolve().is_relative_to(scratch):
        base = scratch / "pytest"
        base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="check-root-", dir=base) as directory:
        yield Path(directory)


@pytest.fixture
def private_directory():
    """An owner-only throwaway directory for isolated runtime checks."""
    if (
        os.name != "posix"
        or not Path("/var/tmp").is_dir()
        or Path("/var").is_symlink()
        or Path("/var/tmp").is_symlink()
    ):
        pytest.skip("a private bus needs a physical /var/tmp")
    with tempfile.TemporaryDirectory(prefix="ac-private-", dir="/var/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def private_comms(private_directory):
    """Install one isolated SQLite bus and select it for this check's lifetime."""
    from bus_record_fixture import prepare_native_comms

    root = private_directory / "wire"
    comms = prepare_native_comms(Comms(root))
    from agent_comms.launch_root import LaunchRoot

    with LaunchRoot.from_path(root).selected():
        yield comms


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "comms"


@pytest.fixture
def configured_repository(durable_home):
    """Own the temporary commander's config and its real plan-bearing checkout."""
    from agent_comms.commander.config import CommanderConfig
    from agent_comms.repo import Repo

    repository = Repo.of(Path(__file__).resolve().parents[1])

    def configure(bus: Comms, *check_repositories: Repo) -> CommanderConfig:
        config = CommanderConfig.on_machine(
            'lead', (*check_repositories, repository), bus.root, durable_home,
        )
        config.write(durable_home)
        return config

    return configure


@pytest.fixture
def installed_runtime(durable_home, tmp_path):
    """A temporary installation record, executable and pointer, never the real home."""
    from agent_comms.installer.runtime_resolution import (
        CommitRecord, GitRecord, PackageRecord, RuntimePackage, RuntimeResolution,
    )
    from agent_comms.launch_runtime import LaunchRuntime
    from agent_comms.repo import Repo

    repository = Repo.of(Path(__file__).resolve().parents[1])
    packages = {}
    for member in RuntimePackage.members_with(RuntimePackage):
        source = SourceRepository.create(tmp_path / 'runtime-sources', member.repository)
        packages[member.distribution] = PackageRecord(
            version='1', source=GitRecord(
                url=str(source.remote), vcs_info=CommitRecord(commit_id=source.main_commit),
            ),
        )
    runtime = tmp_path / 'installed-runtime'
    (runtime / 'bin').mkdir(parents=True)
    (runtime / 'bin/python').symlink_to(sys.executable)
    RuntimeResolution(
        core_commit=repository.git(repository.checkout, 'rev-parse', 'HEAD').strip(),
        packages=packages,
    ).save(runtime)
    current = durable_home / '.local/share/agent-comms/current'
    current.parent.mkdir(parents=True)
    current.symlink_to(runtime, target_is_directory=True)
    return LaunchRuntime.installed()


def declare_test_bus(comms: Comms, *, channels=(), threads=()) -> Comms:
    """Declare exactly the test's named channels and members, never inferred ones."""
    for channel in channels:
        comms.channels.create_tag(channel)
    for thread in threads:
        comms.registry.declare(thread)
    return comms


@pytest.fixture
def comms(root: Path) -> Comms:
    Bus.install(Bus.path_in(root))
    return Comms(root)


@pytest.fixture
def bus(tmp_path):
    """A freshly installed bus, shared by the record and recipient test families."""
    return Bus.install(Bus.path_in(tmp_path))


@pytest.fixture
def thread_registration(tmp_path_factory):
    from agent_comms.registration import Registration
    return Registration(tmp_path_factory.mktemp('thread-registration') / 'registry.json')


@pytest.fixture
def wired(comms: Comms) -> Comms:
    """A wire with PR111 (parent) and fixer (child) registered."""
    return declare_test_bus(comms, channels=("base", "auth"), threads=(
        comms.registry.new_thread(name='PR111', tags=frozenset({'base'}), worktree='/tmp/wt1'),
        comms.registry.new_thread(name='fixer', tags=frozenset({'auth'}), worktree='/tmp/wt1', parent=ForkedParent('PR111'), task=GivenThreadText('fix auth')),
    ))


