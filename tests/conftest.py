"""Shared fixtures: an isolated Comms wire per test."""
from agent_comms.native_package import pinned_native_package

from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.selected_session import SelectedSession
import asyncio
import os
import tempfile
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from agent_comms.bus_publication import AgentOrigin
from agent_comms.comms import Comms
from agent_comms.message_bus import MessageBus
from agent_comms.native_tool_transport import ToolListener
from agent_comms.publisher import ParticipantPublisher, Publisher
from agent_comms.registration import Registration
from agent_comms.messages import ConversationRole, InfoKind, Message
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from native_backend_fixture import native_backend_fixture


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
    """Stage an agent's initial message on the bus; no SQL acceptance or live wake."""
    type.require_sendable()
    with _store_lock(comms.root / "wire"):
        return comms.bus.publisher.publish_initial_cohort(
            Message(sender=sender, target=target, body=body, type=type, role=role),
            origin=AgentOrigin(),
        )

def pytest_addoption(parser):
    from agent_comms.storage_rules import StorageRule

    parser.addoption("--native-cache", type=Path, default=StorageRule.native_cache,
                     help="Root of the native test environments stack/bin/pi-native-test-env builds")


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
def source_reads():
    from agent_comms.native_entries import NativeEvidenceScope

    with NativeEvidenceScope() as scope:
        yield scope


@pytest.fixture
def cursor_delivery(source_reads):
    from agent_comms.cursor_publication import CursorDelivery

    return CursorDelivery(source_reads=source_reads)


@pytest.fixture(scope="session")
def native_runtime_package(pytestconfig):
    """The artifact of this checkout's own pin from its test environment; the pins must agree."""
    from agent_comms import native_package

    return native_package.VerifiedNativePackage.from_environment(pytestconfig.getoption("native_cache"))


@pytest.fixture(autouse=True)
def _native_runtime_resources_are_explicit(monkeypatch, native_runtime_package):
    """Source tests use the same reviewed native resource as their installed interpreter."""
    from agent_comms import native_package

    monkeypatch.setattr(native_package, "MANIFEST", native_runtime_package.path.parents[3] / "pi-native.sha256")


@pytest.fixture(autouse=True)
def _managed_runtime_environment_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's live managed process is not a test wire or native child."""
    from agent_comms.launch_root import LaunchRoot

    monkeypatch.setattr(LaunchRoot, "_current", LaunchRoot.unstarted())
    for name in _MANAGED_RUNTIME_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _deferred_candidate_scheduler_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep optional background WAL workers out of unrelated test temp cleanup.

    tests/test_candidate_maintenance.py opts back in for the production hook.
    This does not affect fresh subprocess processes used by integration tests.
    """
    monkeypatch.setattr(
        "agent_comms.candidate_maintenance.schedule_candidate_catchup", lambda *_: None
    )


@pytest.fixture
def private_directory():
    """A throwaway directory on physical /var/tmp; private N/K publication refuses /tmp."""
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
def declared_native_package(tmp_path, monkeypatch):
    """A construction-only artifact with real layout, ancestry and tree verification."""
    from agent_comms import native_package

    deployment = tmp_path / "native"
    deployment.mkdir(mode=0o700)
    package = deployment / "node_modules/@earendil-works/pi-coding-agent"
    (package / "dist").mkdir(parents=True)
    (package / "dist/cli.js").write_text("// This fixture never starts Node.\n")
    from agent_comms.native_tools import NativeToolDeclaration

    for tool in NativeToolDeclaration.members_with(NativeToolDeclaration):
        extension = tool.native_extension(package)
        extension.parent.mkdir(parents=True, exist_ok=True)
        extension.write_text("// Construction-only native tool extension.\n")
    manifest = tmp_path / "pin"
    manifest.write_text(native_package.TREE_PREFIX + native_package.package_tree_digest(package) + "\n")
    monkeypatch.setattr(native_package, "MANIFEST", manifest)
    pinned = native_package.pinned_location()
    deployment.rename(pinned.parents[2])
    return pinned


@pytest.fixture
def verified_native_package(declared_native_package):
    from agent_comms.native_package import VerifiedNativePackage

    return VerifiedNativePackage.acquire(declared_native_package)


@pytest.fixture
def configured_catalog(verified_native_package, tmp_path, monkeypatch):
    """Unit catalogs own an explicit artifact, not the developer interpreter's runtime."""
    from agent_comms.config_options import ConfigOptions
    from agent_comms.native_arguments import NativeArguments

    catalog = ConfigOptions(
        NativeArguments.default_binary, NativeArguments.parse(()),
        RestartEnvironment(native_config=tmp_path / "config", agent_directory=tmp_path / "config"),
        lambda: verified_native_package,
    )
    monkeypatch.setattr(ConfigOptions, "configured", classmethod(lambda cls: catalog))
    return catalog


@pytest.fixture
def private_comms(private_directory):
    """One throwaway private bus, built as production builds one.

    Its launch derives like any private root's: the marker installed here
    gives the root ID, this installation the package.
    """
    root = private_directory / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    return comms


@pytest.fixture
def delayed_launch(monkeypatch: pytest.MonkeyPatch):
    """Hold the first real exec verification; later launches run unchanged."""
    import asyncio
    import sys

    from agent_comms.child_process import Platform

    with Platform.current().launch((sys.executable, "-c", "pass"), (), LaunchRuntime.running()) as launch:
        launch_type = type(launch)
    original_verify = launch_type.verify_async
    original_release = launch_type.release
    captured, verified = [], []

    def release(launch, identity):
        captured.append(identity)
        original_release(launch, identity)

    async def verify(launch):
        await original_verify(launch)
        verified.append(True)
        if len(verified) == 1:
            await asyncio.Event().wait()

    monkeypatch.setattr(launch_type, "release", release)
    monkeypatch.setattr(launch_type, "verify_async", verify)
    return captured, verified


@pytest.fixture
def fake_native_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared native choices for tests whose child/provider edge is controlled."""
    from agent_comms.pi_payloads import ThinkingLevelsData
    from agent_comms.pi_vocabulary import ThinkingLevel

    monkeypatch.setenv(
        "AGENT_COMMS_AGENT_MODELS",
        ",".join((
            "openrouter/z-ai/glm-5.3-flash",
            "openrouter/deepseek/deepseek-v4.1-flash",
            "response-local/fixture",
            "response-local/second",
            "test/model",
        )),
    )

    async def thinking_levels(*_args, **_kwargs):
        return ThinkingLevelsData(
            levels=tuple(
                member
                for member in ThinkingLevel.members_with(ThinkingLevel)
                if member.ordinary_choice
            )
        )

    monkeypatch.setattr(
        "agent_comms.pi_commands.GetAvailableThinkingLevels.discover", thinking_levels
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "comms"


def declare_test_bus(comms: Comms, *, channels=(), threads=()) -> Comms:
    """Declare exactly the test's named channels and members, never inferred ones."""
    for channel in channels:
        comms.channels.create_tag(channel)
    for thread in threads:
        comms.registry.declare(thread)
    return comms


@pytest.fixture
def comms(root: Path) -> Comms:
    return Comms(root)


@pytest.fixture
def commander(comms: Comms, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A commander thread "lead" holding the owner request "request" and a unit "worker" holding an
    ordinary goal, called as lead. GitHub lists no pull requests to its orders' progress."""
    from agent_comms.goal_actions import SetGoalAction
    from agent_comms.goals import RequestGoalOrder
    from agent_comms.todos import PullRequests
    from agent_comms.tool_caller import ThreadCaller

    monkeypatch.setattr(PullRequests, "read", lambda self, repo, state, order: ())

    declare_test_bus(comms, channels=("comms",), threads=(
        comms.registry.new_thread(name, frozenset({'comms'}), str(tmp_path), created_at=float(created))
        for created, name in enumerate(("lead", "worker"), 1)
    ))
    comms.goals.update_goal("worker", SetGoalAction(text="Original commander brief", goal_id="ongoing"))
    comms.goals.update_goal("lead", SetGoalAction(text="Owner request", goal_id="request", order=RequestGoalOrder()))
    from agent_comms.commander.config import CommanderConfig
    CommanderConfig(commander="lead", root=comms.root,
        state=tmp_path / "state", board=tmp_path / "board.md", narrative=tmp_path / "narrative.md",
        repos=(), worktrees=tmp_path / "wt", scratch=tmp_path / "scratch",
        disk=tmp_path).write(tmp_path)
    return comms, ThreadCaller("lead")


@pytest.fixture
def commander_script(commander, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The order script installed for lead on the commander bus, with a git checkout its orders check."""
    import importlib
    import subprocess
    import sys

    from agent_comms.commander.config import CommanderConfig

    comms, _ = commander
    installed = LaunchRuntime.current_pointer().resolve(strict=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    pointer = LaunchRuntime.current_pointer()
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(installed)
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (("init", "-q"), ("remote", "add", "origin", "https://github.com/OpenHCSDev/agent-comms.git")):
        subprocess.run(["git", "-C", str(repo), *command], check=True)
    from agent_comms.commander import order
    context = order.Commander.read_root(comms.root)
    from agent_comms.rule_reviews import DrawnRuleReviewEntry, AnsweredRuleReviewEntry
    from agent_comms.tools import CommsRuleReviewTool
    from agent_comms.tool_caller import ThreadCaller
    caller = ThreadCaller('lead')
    caller.invoke(CommsRuleReviewTool(entry=DrawnRuleReviewEntry(('Read the command root from its one declared owner.',))), comms)
    caller.invoke(CommsRuleReviewTool(entry=AnsweredRuleReviewEntry(1, '"Read the command root from its one declared owner." P701')), comms)
    yield context, repo


@pytest.fixture
def tool_watch(commander_script):
    """The tool-call watcher reading the commander config commander_script installed."""
    import importlib

    from agent_comms.commander import judge, tool_watch
    return tool_watch


@pytest.fixture
def thread_registration(tmp_path_factory):
    from agent_comms.registration import Registration
    return Registration(tmp_path_factory.mktemp('thread-registration') / 'registry.json')


@pytest.fixture
def wired(comms: Comms) -> Comms:
    """A wire with PR111 (parent) and fixer (child) registered."""
    return declare_test_bus(comms, channels=("base", "auth"), threads=(
        comms.registry.new_thread(name='PR111', tags=frozenset({'base'}), worktree='/tmp/wt1'),
        comms.registry.new_thread(name='fixer', tags=frozenset({'auth'}), worktree='/tmp/wt1', parent='PR111', task='fix auth'),
    ))


@pytest.fixture
def selected_model(monkeypatch, tmp_path, native_runtime_package):
    """Declare a source-test runtime; native validation and package checks stay real.

    Its CLI helpers must consume this Core, not an older installed console script.
    These entrypoint scripts reuse the existing interpreter; this is not a venv.
    """
    import shlex
    import sys

    import agent_comms.native_pi as native_pi
    from agent_comms.native_arguments import NativeArguments, SelectedModel
    from native_backend_fixture import NativeBackendFixture

    runtime = LaunchRuntime(tmp_path / "native-runtime/bin/python")
    runtime.interpreter.parent.mkdir(parents=True)
    source = str(Path(native_pi.__file__).parents[1])
    runtime.interpreter.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n'
    )
    for name, module in (("pi-comms-native", "agent_comms.native_pi"),
                         ("agent-comms", "agent_comms.cli")):
        entrypoint = runtime.interpreter.with_name(name)
        entrypoint.write_text(
            f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {source!r})\n"
            "from pathlib import Path\nimport agent_comms.native_package as native_package\n"
            f"native_package.MANIFEST = Path({str(native_runtime_package.path.parents[3] / 'pi-native.sha256')!r})\n"
            f"from {module} import main\nraise SystemExit(main())\n"
        )
        entrypoint.chmod(0o700)
    runtime.interpreter.chmod(0o700)
    monkeypatch.setattr(LaunchRuntime, "running", classmethod(lambda cls: runtime))
    settings = NativeArguments.parse(NativeBackendFixture.native_arguments())
    monkeypatch.setenv(NativeArguments.binary_key, NativeArguments.owner_launcher(runtime))
    monkeypatch.setenv(NativeArguments.arguments_key, shlex.join(settings.argv))
    return SelectedModel.decode(settings.model)


@pytest.fixture
async def native_backend(tmp_path_factory, monkeypatch, selected_model, native_runtime_package):
    monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
    # Native originals belong to the persistent run, even when a consuming
    # module supplies a separate auto-deleting sealed-bus tmp_path fixture.
    from agent_comms.launch_root import LaunchRoot

    async with native_backend_fixture(tmp_path_factory.mktemp("saved-native-owner"), package=native_runtime_package) as owner:
        with LaunchRoot.from_path(owner.root).selected():
            yield owner


@pytest.fixture
def native_rpc_fixture(monkeypatch):
    """Explicit executable trust for local recorded-protocol fixtures only.

    Production launch attestation is exercised separately; these fixtures test
    native input/event contracts with actual child pipes and no provider.
    """
    import os

    from agent_comms.native_arguments import NativeArguments
    from agent_comms.native_pi import NativePiRpcLaunch

    original = NativePiRpcLaunch.managed
    original_retained = NativePiRpcLaunch.retained_managed

    def prepare(
        command, arguments, *, package, worktree, root, directory, environment=None, session_file=None
    ):
        script = Path(command)
        if not script.is_file():
            return original(
                command,
                arguments,
                package=package,
                worktree=worktree,
                root=root,
                directory=directory,
                environment=environment,
                session_file=session_file,
            )
        args = NativeArguments.parse(arguments).rpc()
        if session_file:
            args += ("--session", session_file)
        inherited = RestartEnvironment.inherit(os.environ)
        env = inherited.encode()
        env.update(environment or {})
        return NativePiRpcLaunch(
            (command, *args),
            Path(worktree),
            env,
            SelectedSession.for_launch(
                Path(directory),
                Path(session_file) if session_file is not None else None,
            ),
            package,
            root=Path(root), runtime=LaunchRuntime.running(), configuration=RestartEnvironment.inherit(env),
        )

    def retained(
        launch, command, arguments, *, worktree, root, directory, environment=None, session_file=None
    ):
        if (
            Path(command).is_file()
            and Path(command).resolve() == Path(launch.argv[0]).resolve()
            and Path(worktree).resolve() == launch.cwd.resolve()
            and Path(directory) == launch.session.directory
            and (
                Path(session_file).resolve() if session_file is not None else None
            ) == launch.session.path
        ):
            return launch
        return original_retained(
            launch,
            command,
            arguments,
            worktree=worktree,
            root=root,
            directory=directory,
            environment=environment,
            session_file=session_file,
        )

    monkeypatch.setattr(NativePiRpcLaunch, "managed", prepare)
    monkeypatch.setattr(NativePiRpcLaunch, "retained_managed", retained)
