"""Shared fixtures: an isolated Comms wire per test."""


from dataclasses import dataclass
import asyncio
from threading import Thread
from contextlib import asynccontextmanager
import fcntl
import json
import os
import pwd
import socket
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from importlib.metadata import distribution
from pathlib import Path

import pytest
from pydantic import JsonValue

# First: importing it removes the caller's repository from the environment before any git runs.
import fixture_git  # noqa: F401
import goal_owner_fixture
from agent_comms.cli_bridges.session_watch import WatchClaim
from agent_comms.comms import Comms, wire
from agent_comms.execution.cursor_publication import CursorDelivery
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.native_entries import NativeEvidenceScope, SessionEntry
from agent_comms.execution.pi_durable_backend import PiDurableBackend
from agent_comms.installer.native_package import (
    VerifiedNativePackage,
    pinned_location,
    pinned_native_package,
)
from agent_comms.installer.probes.interpreter import InterpreterBuildProbe, RecordedInterpreterBuild
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.messaging.bus import Bus, BusRecord
from agent_comms.messaging.bus_publication import AgentOrigin
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.messages import (
    ConversationRole,
    InfoKind,
    Message,
    MessageKind,
    MessageRole,
)
from agent_comms.messaging.publisher import InitialWrites, ParticipantPublisher
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.pi_payloads import PiMessage
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import RegisteredTeamProject
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.thread_text import ForkedParent, GivenThreadText
from agent_comms.registry.threads import NativeThread, Thread
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceClaimHolder,
    MaintenanceHolder,
    MaintenanceOperator,
)
from agent_comms.storage.typed_table import TypedTable
from agent_comms.team.order import Team
from agent_comms.team.pi_rule_gate import PiRedeal, PiRuleGate
from agent_comms.team.rule_deal import ReadSpan, RuleDeal, SessionAudit, StartedDeal
from agent_comms.transport.native_tool_transport import ToolListener
from agent_comms.transport.core_access import LocalCoreAccess
from agent_comms.transport.tool_caller import PeerCaller
from agent_comms.ui_model.observation import LocalRouteObservation, ObservationProcess
from agent_comms.transport.tool_declaration import NoToolRecord, ToolDeclaration
from agent_comms_hooks.watch import WatchRecord
from source_programs import SourcePrograms
from source_revision_fixture import SourceRepository

pytest_plugins = (goal_owner_fixture.__name__, "github_fixture", "owner_socket_fixture", "pytest_session")


class TranscriptRecords:
    """Fixed test time, projected through each record's declared constructor."""

    def __init__(self) -> None:
        from agent_comms.nominal.field_codec import RecordedTimestamp

        self.timestamp = RecordedTimestamp("2026-10-01T00:00:00.000Z")

    def event[Event](self, declaration: Callable[..., Event], *args: str, **facts: JsonValue | ToolDeclaration | NoToolRecord) -> Event:
        return declaration(*args, timestamp=self.timestamp.at.timestamp(), **facts)

    def entry[Entry](self, declaration: Callable[..., Entry], **facts: JsonValue | PiMessage) -> Entry:
        return declaration(timestamp=self.timestamp, **facts)

    def message(self, payload: dict[str, JsonValue], **coordinates: JsonValue) -> dict[str, JsonValue]:
        from agent_comms.execution.native_entries import MessageEntry
        from agent_comms.pi_payloads import PiMessage

        entry = self.entry(MessageEntry, message=PiMessage.from_wire(payload), **coordinates)
        encoded = FieldCodec.decode(dict[str, JsonValue], entry.to_wire())
        return {**encoded, "message": payload}

    def session(self, id: str, **coordinates: JsonValue) -> SessionEntry:
        from agent_comms.execution.native_entries import SessionEntry
        from agent_comms.execution.native_session_reopen import PiSessionId

        return self.entry(SessionEntry, id=FieldCodec.decode(PiSessionId, id), **coordinates)


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

    from agent_comms.coordination.cohort import sealed_cohort_assignments
    from agent_comms.coordination.cohort_foreground import _accept_visible_deliveries

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
    install names, which on 2026-10-24 was a stale checkout one TurnFailure short.

    PYTHONPATH chooses the source; the venv's entry points and console scripts are a copy
    of whichever pyproject was last installed. A copy from another checkout launches moved
    modules by their old path (on 2026-11-09, ten host_services tests ran an
    agent-comms-connector importing agent_comms.connector after it moved to transport/), so
    the session refuses to run until the venv's copy matches this checkout's declaration."""
    from xdist import is_xdist_worker

    os.environ["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in session.config.getini("pythonpath"))
    if is_xdist_worker(session):
        return
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    declared = {("console_scripts", name, value) for name, value in project["scripts"].items()}
    declared |= {(group, name, value) for group, points in project["entry-points"].items()
                 for name, value in points.items()}
    installed = {(point.group, point.name, point.value)
                 for point in distribution("agent-comms").entry_points}
    if declared != installed:
        raise pytest.UsageError(
            f"This venv's agent-comms entry points were installed from another pyproject "
            f"than {root / 'pyproject.toml'}: missing {sorted(declared - installed)}, "
            f"stale {sorted(installed - declared)}. Rebuild it from this checkout: "
            f"toad's bin/toad-test-env --agent-comms {root}, or `uv pip install -e {root}`.")
    if session.config.option.collectonly:
        return
    from agent_comms.projects.checkouts import Checkout

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
def undealt_gate(tmp_path: Path) -> PiRuleGate:
    """A Pi rule gate never dealt any rules, so it lets every call run."""
    return PiRuleGate(tmp_path / "scratch", tmp_path)


@pytest.fixture
def manager_socket(tmp_path: Path):
    """One real filesystem socket, even under a basetemp longer than sockaddr_un."""
    runtime = tmp_path / "user-runtime"
    directory = runtime / "systemd"
    directory.mkdir(parents=True)
    with socket.socket(socket.AF_UNIX) as manager:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            manager.bind(f"/proc/self/fd/{descriptor}/private")
        finally:
            os.close(descriptor)
        yield runtime


@pytest.fixture
def user_manager(manager_socket: Path, monkeypatch: pytest.MonkeyPatch):
    """Admit a private manager socket through the real selection boundary. Only the OS
    account record is substituted; no Core manager method is replaced."""
    from agent_comms.execution.child_process import SystemdUserManager

    account = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: pwd.struct_passwd(
        (*account[:5], str(Path.home()), account.pw_shell)))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(manager_socket))
    return SystemdUserManager(manager_socket)


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
    from agent_comms.execution.native_entries import NativeEvidenceScope

    with NativeEvidenceScope() as scope:
        yield scope


@pytest.fixture
def cursor_delivery(source_reads: NativeEvidenceScope) -> CursorDelivery:
    from agent_comms.execution.cursor_publication import CursorDelivery

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
    from agent_comms.installer.deal_prompt import DealPrompt
    from agent_comms.installer.native_package import VerifiedNativePackage

    package = VerifiedNativePackage.qualify(pinned_native)
    DealPrompt.prepare()
    return package


@pytest.fixture(autouse=True)
def _test_worker_hosts_stop_with_the_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Every worker host a test started on its private bus stops when the test ends."""
    if "tmp_path" not in request.fixturenames:
        yield
        return
    from worker_hosts import TestWorkerHosts

    hosts = TestWorkerHosts(request.getfixturevalue("tmp_path"))
    yield
    hosts.stop()


@pytest.fixture(autouse=True)
def _managed_runtime_environment_is_explicit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A developer's live managed process is not a test wire or native child."""
    from agent_comms.execution.launch_root import LaunchRoot

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
    from agent_comms.projects.storage_rules import StorageRule

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
    from agent_comms.execution.launch_root import LaunchRoot
    from agent_comms.installer.logs import InstallLogs

    try:
        with LaunchRoot.from_path(root).selected():
            yield comms
    finally:
        InstallLogs(root).stop_running()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "comms"


@pytest.fixture
def configured_repository(durable_home: Path, github_cli) -> Callable[[Comms, *tuple[Repo, ...]], Team]:
    """Declare the temporary colleague on its bus and use its real plan-bearing checkout."""
    from agent_comms.projects.repo import Repo
    from agent_comms.team.order import Team

    repository = Repo.of(Path(__file__).resolve().parents[1])

    def configure(bus: Comms, *check_repositories: Repo) -> Team:
        github_cli.configure(*(repo.github for repo in (*check_repositories, repository)))
        return Team.on_root(
            'colleague', (*check_repositories, repository), bus.root,
        )

    return configure


@pytest.fixture
def interpreter_build() -> RecordedInterpreterBuild:
    """The native implementation tests execute under, also symlinked by fixture runtimes.

    This observes the real current interpreter; it does not claim a process ran
    inside the synthetic installation's package environment.
    """
    return InterpreterBuildProbe().run()


@pytest.fixture
def installed_runtime(
    durable_home: Path, tmp_path: Path, interpreter_build: RecordedInterpreterBuild,
) -> LaunchRuntime:
    """A temporary installation record, executable and pointer, never the real home."""
    from agent_comms.installer.probes.packages import CommitRecord, GitRecord, PackageRecord
    from agent_comms.installer.runtime_resolution import RuntimePackage, RuntimeResolution
    from agent_comms.projects.repo import Repo

    repository = Repo.of(Path(__file__).resolve().parents[1])
    packages = {}
    for member in RuntimePackage.members_with(RuntimePackage):
        source = SourceRepository.create(tmp_path / 'runtime-sources', member.repository)
        packages[member.distribution] = PackageRecord(
            version='1', source=GitRecord(
                url=str(source.remote_path), vcs_info=CommitRecord(commit_id=source.main_commit),
            ),
        )
    return record_installed_runtime(tmp_path / 'installed-runtime', RuntimeResolution(
        core_commit=repository.git(repository.checkout, 'rev-parse', 'HEAD').strip(),
        packages=packages, interpreter=interpreter_build,
    ))


def record_installed_runtime(runtime: Path, resolution: RuntimeResolution) -> LaunchRuntime:
    """The one test writer of an installation: interpreter, resolution and HOME's pointer.

    A later record re-points the pointer, as a reinstall does, so fixtures that each install
    a build can share one HOME.
    """
    from agent_comms.execution.launch_runtime import LaunchRuntime

    (runtime / 'bin').mkdir(parents=True)
    (runtime / 'bin/python').symlink_to(sys.executable)
    resolution.save(runtime)
    pointer = LaunchRuntime.current_pointer()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.unlink(missing_ok=True)
    pointer.symlink_to(runtime, target_is_directory=True)
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

    from agent_comms.execution.turn_failure import TurnFailure
    from agent_comms.planning.goal_ledger import FailedTurnEvidence
    from agent_comms.planning.goal_attempts import GoalAttemptStore
    from agent_comms.storage.typed_table import sql_literal

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
    from agent_comms.projects.storage_rules import StorageRule

    Bus.install(Bus.path_in(root))
    StorageRule(floor_gib=0).prepare_isolated(root)
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
    from agent_comms.registry.registration import Registration
    return Registration(RegistryFile.path_in(tmp_path_factory.mktemp('thread-registration')))


@pytest.fixture
def wired(comms: Comms) -> Comms:
    """A wire with PR111 (parent) and fixer (child) registered."""
    return declare_test_bus(comms, channels=("base", "auth"), threads=(
        NativeThread(name='PR111', tags=frozenset({'base'}), worktree=Checkout(Path('/tmp/wt1'))),
        NativeThread(name='fixer', tags=frozenset({'auth'}), worktree=Checkout(Path('/tmp/wt1')), parent=ForkedParent('PR111'), task=GivenThreadText('fix auth')),
    ))




def team_as(root: Path, teammate: str) -> Team:
    """A team verb's context acting as ``teammate``, the CLI's ``--as``: no verb acts without
    naming the teammate running it."""
    return Team.read_root(root).model_copy(update={"actor": PeerCaller(teammate)})


def declare_team(comms: Comms, colleague: Thread) -> Comms:
    """The bus's team project with `colleague` as a teammate: the one test owner of a team."""
    comms.registry.declare(colleague)
    comms.registry.set_team_project(RegisteredTeamProject(()))
    return comms


def answer_rules(comms: Comms, name: str) -> None:
    """Deal a Pi teammate its team rules and answer them, as a teammate does before its first tool:
    every dealt file read in full, then an audit answering every prompt and probe."""
    deal = PiRedeal.of(comms, comms.registry.require(name))
    deal.redeal(StartedDeal())
    answer_deal(deal)


def probe_answers(owner_words: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """One answer per probe a deal with these recorded prompts asks, each citing a real line of
    this checkout and, when he said something, quoting his own words."""
    answers = [
        {"probe": "hunt", "pattern": "MEMB-1", "fact": "the six rule names are listed by hand in order",
         "at": "src/agent_comms/team/package.py:15", "derive": "declaration",
         "by": "src/agent_comms/team/package.py:15"},
        {"probe": "flinch", "answer": "No: a fixture session resists nothing; the gate it runs is"
                                      " src/agent_comms/team/rule_deal.py:1"},
    ]
    if owner_words:
        answers.append({"probe": "prior", "words": owner_words[0],
                        "prior": "a fixture quotes its owner to prove the gate reads him",
                        "refuted_by": "Corollary 4.12"})
    return answers


def fresh_confession(deal: RuleDeal) -> bool:
    """The deal's audit holds no answer: it is the blank skeleton the deal wrote."""
    return SessionAudit.read(deal.audit) == deal.current().due.skeleton()


def answer_deal(deal: PiRedeal) -> None:
    """Read every file of the current deal in full, then write the audit answering the deal."""
    record = deal.current()
    for source in record.dealt:
        deal.record_read(source, ReadSpan(1, sys.maxsize, sys.maxsize))
    deal.audit.write_text(json.dumps({
        "owner_words": [{"words": words, "doing": "testing"} for words in record.due.owner_words],
        "probes": probe_answers(record.due.owner_words),
    }))


@pytest.fixture
def team_bus(tmp_path):
    """A team bus with the teammates `claude-code` and `unit-x`. Its
    storage rule keeps scratch under the bus root, so no deal reaches the developer's home
    whatever HOME holds when the bus is built."""
    bus = declare_team(installed_comms(tmp_path / "bus"), NativeThread(
        "claude-code", frozenset(), Checkout(tmp_path),
        model=DeclaredModel("anthropic/claude-opus-4-6")))
    bus.registry.declare(NativeThread(
        "unit-x", frozenset(), Checkout(tmp_path),
        model=DeclaredModel("anthropic/claude-opus-4-6")))
    return bus


@pytest.fixture
def team_session(tmp_path, monkeypatch, team_bus):
    """Claude session `s` bound to the teammate `claude-code`, so hooks see the thread the
    registry holds."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    WatchClaim("s").enrol(WatchRecord("claude-code", str(team_bus.root), (), tmp_path / "state"))
    return team_bus


@dataclass(frozen=True)
class UiCoreReads:
    """A UI's own Core calls, issued the way a UI issues them: from its thread, never from
    the delivery loop, which `ObserverSameLoop` refuses because it would await its own answer.

    Nineteen tests built `LocalCoreAccess(comms)` by hand, so the day a local read needed the
    child serving this root, each of them was its own edit and its own chance to forget the
    rule. One owner holds both here.
    """

    access: LocalCoreAccess

    async def call[R](self, request):
        return await asyncio.to_thread(self.access.call, request)


@asynccontextmanager
async def ui_core_reads(comms: Comms) -> AsyncIterator[UiCoreReads]:
    """For a test that builds its own bus; the fixture below is the shared-bus case."""
    async with ObservationProcess.awaited(comms.route) as reads:
        yield UiCoreReads(LocalCoreAccess(comms, reads))


def ui_core_read[R](comms: Comms, request):
    """One Core call as a UI makes it, for a synchronous test that owns its own bus.

    The child answers on the delivery loop while this read waits on a worker thread, which
    is the arrangement `ObserverSameLoop` exists to require.

    One child per read. Holding one open for a whole test needs the delivery loop on a
    background thread, and two things stop that today: `ObservationProcess.awaited` takes
    the loop and spawns the child together, although `start_receiving` documents that only
    attachment belongs on the loop; and a closed process can stop the loop it was attached
    to, so the next `run_coroutine_threadsafe` waits on a loop whose thread has already
    exited. Splitting construction from attachment and retiring the loop with its children
    both still hang at the second child (test_thread_names.py:111). The cost is one process
    per read and it is named in the PR rather than traded for an intermittent hang.
    """

    async def once():
        async with ui_core_reads(comms) as core:
            return await core.call(request)

    return asyncio.run(once())


@pytest.fixture
async def local_core(comms: Comms) -> AsyncIterator[UiCoreReads]:
    async with ui_core_reads(comms) as reads:
        yield reads
