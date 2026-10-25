"""Shared fixtures: an isolated Comms wire per test."""


import asyncio
import fcntl
import os
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pydantic import JsonValue
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_payloads import PiMessage
from agent_comms.native_entries import SessionEntry
from agent_comms.tool_declaration import ToolDeclaration, NoToolRecord
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path

import pytest

# First: importing it removes the caller's repository from the environment before any git runs.
import fixture_git  # noqa: F401
import goal_owner_fixture
from agent_comms.bus import Bus, BusRecord
from agent_comms.bus_publication import AgentOrigin
from agent_comms.channel_targets import CommandTarget
from agent_comms.comms import Comms, wire
from agent_comms.locked_store import RegistryFile
from agent_comms.maintenance_barrier import (
    MaintenanceBarrier, MaintenanceClaimHolder, MaintenanceHolder, MaintenanceOperator,
)
from agent_comms.message_bus import MessageBus
from agent_comms.messages import ConversationRole, InfoKind, Message, MessageKind, MessageRole
from agent_comms.native_package import pinned_location, pinned_native_package
from agent_comms.native_tool_transport import ToolListener
from agent_comms.pi_durable_backend import PiDurableBackend
from agent_comms.publisher import InitialWrites, ParticipantPublisher, Publisher
from agent_comms.registration import Registration
from agent_comms.thread_text import ForkedParent, GivenThreadText
from agent_comms.typed_table import TypedTable
from agent_comms.native_entries import NativeEvidenceScope
from agent_comms.cursor_publication import CursorDelivery
from agent_comms.native_package import VerifiedNativePackage
from agent_comms.commander.order import Commander
from agent_comms.repo import Repo
from agent_comms.launch_runtime import LaunchRuntime
from source_programs import SourcePrograms
from source_revision_fixture import SourceRepository
from agent_comms.threads import Thread

pytest_plugins = (goal_owner_fixture.__name__, "github_fixture")


class TranscriptRecords:
    """Fixed test time, projected through each record's declared constructor."""

    def __init__(self) -> None:
        from agent_comms.field_codec import RecordedTimestamp

        self.timestamp = RecordedTimestamp("2026-10-01T00:00:00.000Z")

    def event[Event](self, declaration: Callable[..., Event], *args: str, **facts: JsonValue | ToolDeclaration | NoToolRecord) -> Event:
        return declaration(*args, timestamp=self.timestamp.at.timestamp(), **facts)

    def entry[Entry](self, declaration: Callable[..., Entry], **facts: JsonValue | PiMessage) -> Entry:
        return declaration(timestamp=self.timestamp, **facts)

    def message(self, payload: dict[str, JsonValue], **coordinates: JsonValue) -> dict[str, JsonValue]:
        from agent_comms.native_entries import MessageEntry
        from agent_comms.pi_payloads import PiMessage

        entry = self.entry(MessageEntry, message=PiMessage.from_wire(payload), **coordinates)
        encoded = FieldCodec.decode(dict[str, JsonValue], entry.to_wire())
        return {**encoded, "message": payload}

    def session(self, **coordinates: JsonValue) -> SessionEntry:
        from agent_comms.native_entries import SessionEntry

        return self.entry(SessionEntry, **coordinates)


transcript_records = TranscriptRecords()


@pytest.fixture
def source_repository(tmp_path: Path) -> SourceRepository:
    return SourceRepository.create(tmp_path / 'source')


@pytest.fixture
def source_programs(tmp_path: Path) -> SourcePrograms:
    return SourcePrograms.create(tmp_path)


@pytest.fixture
def bridge_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
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


async def await_event_or_task[Result, Work](event: Awaitable[Result], task: asyncio.Task[Work]) -> Result:
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
    return wire(root, Publisher)


def responder(root: Path, registry: Registration) -> MessageBus[ParticipantPublisher]:
    """The selected owner publishes replies and releases its attempt's claims."""
    return MessageBus(root, registry, ParticipantPublisher)


def publish_initial(
    comms: Comms, sender: str, target: str, body: str, type: type[MessageKind] = InfoKind,
    *, role: MessageRole = ConversationRole(),
) -> BusRecord:
    """Publish through the ordinary owner without submitting to a native agent."""
    type.require_sendable()
    publisher = comms.bus.publisher
    assert isinstance(publisher, InitialWrites)
    return publisher.publish_ordinary(
        Message(sender=sender, target=target, body=body, type=type, role=role),
        destination=CommandTarget.named(target), origin=AgentOrigin(),
    )


def accept_initial(bus: MessageBus, root_id: str, seq: int, store) -> tuple:
    """Accept one initial the way its recipient's owner does, then read its sealed claims.

    The live path is the owner's visible-delivery scan (cohort_foreground); it seals
    the whole cohort of every original it finds, so one recipient's scan is enough.
    """
    from concurrent.futures import ThreadPoolExecutor

    from agent_comms.cohort_foreground import _accept_visible_deliveries
    from agent_comms.coordination_cohort import sealed_cohort_assignments

    recipients = bus.bus.delivery(seq).recipients
    if recipients:
        owner = recipients[0]
        with ThreadPoolExecutor(1) as pool:
            pool.submit(asyncio.run, _accept_visible_deliveries(
                bus, root_id, Path(store.session.path), owner.recipient_lookup,
                owner_name=owner.canonical_thread,
            )).result()
    return tuple(
        assignment for recipient in recipients
        for assignment in sealed_cohort_assignments(
            store, recipient.recipient_lookup, after_seq=seq - 1, limit=1)
        if assignment.wire_seq == seq
    )

def pytest_sessionstart(session: pytest.Session) -> None:
    """The controller populates this checkout's pinned submodules before any worker runs,
    as every tree of ours is (Checkout). Collection loads declarations only.

    Child processes import this checkout's sources, the ``pythonpath`` pyproject declares:
    a relative or missing PYTHONPATH sends them to whatever tree the venv's editable
    install names, which on 2026-10-24 was a stale checkout one TurnFailure short."""
    os.environ["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in session.config.getini("pythonpath"))
    if session.config.option.collectonly or "PYTEST_XDIST_WORKER" in os.environ:
        return
    from agent_comms.repo import Checkout

    Checkout(Path(__file__).resolve().parents[1]).prepare()


@pytest.fixture
async def tool_listener() -> AsyncIterator[Callable[[Path, str], ToolListener]]:
    """Every private test endpoint has one joined retirement owner."""
    async with AsyncExitStack() as resources:
        def create(directory: Path, token: str) -> ToolListener:
            listener = ToolListener(directory, token)
            resources.push_async_callback(listener.close)
            return listener
        yield create


@pytest.fixture
def durable_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A credential-free durable host never reads the developer's Pi account."""
    home = tmp_path / "home"
    config = home / ".pi" / "agent"
    config.mkdir(parents=True)
    (config / "auth.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def source_reads() -> Iterator[NativeEvidenceScope]:
    from agent_comms.native_entries import NativeEvidenceScope

    with NativeEvidenceScope() as scope:
        yield scope


@pytest.fixture
def cursor_delivery(source_reads: NativeEvidenceScope) -> CursorDelivery:
    from agent_comms.cursor_publication import CursorDelivery

    return CursorDelivery(source_reads=source_reads)


@pytest.fixture(scope="session")
def pinned_native() -> Path:
    """The pinned Pi build, prepared the first time a test asks for it.

    prepare-pi-native keys its build by the pin and reuses a verified one, so an unchanged
    pin costs nothing and a changed pin rebuilds. A run whose tests never ask never builds;
    workers asking together wait on the stack directory's lock and the first one builds.
    """
    stack = Path(__file__).resolve().parents[1] / "stack"
    held = os.open(stack, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        if not pinned_location().is_dir():
            subprocess.run([str(stack / "native/prepare-pi-native"), "--python", sys.executable], check=True)
    finally:
        os.close(held)
    return pinned_native_package()


@pytest.fixture(scope="session")
def durable_host_packages() -> Path:
    """The locked packages the durable host imports from beside it, installed the first
    time a test asks for them.

    npm ci rewrites node_modules/.package-lock.json, so an unchanged lock costs nothing and
    a newer lock reinstalls; workers asking together wait on the host directory's lock.
    """
    module_path = sys.modules[PiDurableBackend.__module__].__file__
    assert module_path is not None
    host = Path(module_path).parent
    installed = host / "node_modules/.package-lock.json"
    locked = host / "package-lock.json"
    held = os.open(host, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        if not installed.is_file() or installed.stat().st_mtime < locked.stat().st_mtime:
            subprocess.run(
                ["npm", "ci", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund"],
                cwd=host, check=True,
            )
    finally:
        os.close(held)
    return host


@pytest.fixture
def test_uv(monkeypatch: pytest.MonkeyPatch) -> Path:
    """The uv the dev extra installs beside this interpreter, first on PATH, as a test
    runtime home requires of its caller (practice.TestHome)."""
    uv = Path(sysconfig.get_path("scripts")) / "uv"
    if not uv.is_file():
        raise FileNotFoundError(f"{uv} is missing: install the dev extra, which pins uv")
    monkeypatch.setenv("PATH", f"{uv.parent}{os.pathsep}{os.environ['PATH']}")
    return uv


@pytest.fixture(scope="session")
def native_runtime_package(pinned_native: Path) -> VerifiedNativePackage:
    """The running interpreter's artifact, qualified by the real installer before tests."""
    from agent_comms.native_package import VerifiedNativePackage
    from agent_comms.unit_instructions import UnitInstructions

    package = VerifiedNativePackage.qualify(pinned_native)
    UnitInstructions.prepare()
    return package


@pytest.fixture(autouse=True)
def _managed_runtime_environment_is_explicit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A developer's live managed process is not a test wire or native child."""
    from agent_comms.launch_root import LaunchRoot

    with LaunchRoot.unstarted().selected():
        for name in _MANAGED_RUNTIME_ENVIRONMENT:
            monkeypatch.delenv(name, raising=False)
        yield


@contextmanager
def without_mutation_guards(db: sqlite3.Connection, table: type[TypedTable]) -> Iterator[None]:
    """Damage disposable rows, restoring their declaration before a checked read."""
    guards = table.triggers()
    for name in guards:
        db.execute(f'DROP TRIGGER "{name}"')
    try:
        yield
    finally:
        for statement in guards.values():
            db.execute(statement)


@pytest.fixture
def scratch_root(durable_home: Path) -> Path:
    """The test's own agent-scratch, derived from the storage rule under the test's HOME."""
    from agent_comms.storage_rules import StorageRule

    root = StorageRule().scratch_base / "pytest" / "check-root"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def private_directory() -> Iterator[Path]:
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
def private_comms(private_directory: Path) -> Iterator[Comms]:
    """Install one isolated SQLite bus; native consumers acquire their own package."""
    from bus_record_fixture import prepare_native_comms

    root = private_directory / "wire"
    comms = prepare_native_comms(wire(root))
    from agent_comms.installer.launch import InstallLogs
    from agent_comms.launch_root import LaunchRoot

    try:
        with LaunchRoot.from_path(root).selected():
            yield comms
    finally:
        InstallLogs(root).stop_running()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "comms"


@pytest.fixture
def configured_repository(durable_home: Path, github_cli) -> Callable[[Comms, *tuple[Repo, ...]], Commander]:
    """Declare the temporary commander on its bus and use its real plan-bearing checkout."""
    from agent_comms.commander.order import Commander
    from agent_comms.repo import Repo

    repository = Repo.of(Path(__file__).resolve().parents[1])

    def configure(bus: Comms, *check_repositories: Repo) -> Commander:
        github_cli.configure(*(repo.github for repo in (*check_repositories, repository)))
        return Commander.on_root(
            'lead', (*check_repositories, repository), bus.root,
        )

    return configure


@pytest.fixture
def installed_runtime(durable_home: Path, tmp_path: Path) -> LaunchRuntime:
    """A temporary installation record, executable and pointer, never the real home."""
    from agent_comms.installer.probes.packages import CommitRecord, GitRecord, PackageRecord
    from agent_comms.installer.runtime_resolution import RuntimePackage, RuntimeResolution
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


@contextmanager
def maintenance_held(root: Path) -> Iterator[MaintenanceHolder]:
    """This process holds ``root``'s maintenance the way an install does, nonce in its
    environment for its children, so a stored schema may change here and nowhere else."""
    barrier = MaintenanceBarrier(RegistryFile.path_in(root))
    held = barrier.begin(MaintenanceOperator.current())
    try:
        with held.holding():
            yield MaintenanceClaimHolder(held.nonce)
    finally:
        barrier.clear(held)


def declare_test_bus(
    comms: Comms, *, channels: tuple[str, ...] = (), threads: tuple[Thread, ...] = (),
) -> Comms:
    """Declare exactly the test's named channels and members, never inferred ones."""
    for channel in channels:
        comms.channels.create_tag(channel)
    for thread in threads:
        comms.registry.declare(thread)
    return comms


def ledger_one_failure_short(root: Path) -> Path:
    """Install 13's ledger: its failure CHECK one TurnFailure member short, the schema an
    install must carry before any opening accepts it."""
    from contextlib import closing

    from agent_comms.goal_attempts import GoalAttemptStore
    from agent_comms.goal_failure_observation import FailedTurnEvidence
    from agent_comms.turn_failure import TurnFailure
    from agent_comms.typed_table import sql_literal

    directory = root / GoalAttemptStore.root_directory
    directory.mkdir(mode=0o700)
    path = GoalAttemptStore.initialize(directory).path
    newest = sql_literal(TurnFailure.names()[-1])
    with closing(sqlite3.connect(path)) as db:
        (table,) = db.execute("SELECT sql FROM sqlite_master WHERE name=?",
                              (FailedTurnEvidence.declared_name,)).fetchone()
        db.execute(f'DROP TABLE "{FailedTurnEvidence.declared_name}"')
        db.execute(table.replace(f", {newest}", ""))
        db.commit()
    return path


def installed_comms(root: Path) -> Comms:
    """A root as the installer leaves it: its bus exists before any reader opens it. A test
    bus of a few megabytes declares no storage floor; tests of the floor declare their own."""
    from agent_comms.storage_rules import StorageRule

    Bus.install(Bus.path_in(root))
    StorageRule.path_in(root).write_text(FieldCodec.compact(FieldCodec.encode(StorageRule(
        volume=root, scratch_base=root / 'scratch', checkout_base=root / 'checkouts',
        floor_gib=0))))
    return wire(root)


@pytest.fixture
def comms(root: Path) -> Comms:
    return installed_comms(root)


@pytest.fixture
def bus(tmp_path: Path) -> Bus:
    """A freshly installed bus, shared by the record and recipient test families."""
    return Bus.install(Bus.path_in(tmp_path))


@pytest.fixture
def thread_registration(tmp_path_factory: pytest.TempPathFactory) -> Registration:
    from agent_comms.registration import Registration
    return Registration(tmp_path_factory.mktemp('thread-registration') / 'registry.json')


@pytest.fixture
def wired(comms: Comms) -> Comms:
    """A wire with PR111 (parent) and fixer (child) registered."""
    return declare_test_bus(comms, channels=("base", "auth"), threads=(
        Thread(name='PR111', tags=frozenset({'base'}), worktree='/tmp/wt1'),
        Thread(name='fixer', tags=frozenset({'auth'}), worktree='/tmp/wt1', parent=ForkedParent('PR111'), task=GivenThreadText('fix auth')),
    ))


