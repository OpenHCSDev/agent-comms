"""Shared fixtures: an isolated Comms wire per test."""

from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.selected_session import SelectedSession
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.threads import Thread


_MANAGED_RUNTIME_ENVIRONMENT = frozenset({
    "AGENT_COMMS_MANAGED",
    "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE",
    "AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID",
    "AGENT_COMMS_PROJECT_REQUEST",
    "AGENT_COMMS_PROJECT_SOCKET",
    "AGENT_COMMS_ROOT",
    "AGENT_COMMS_SELECTED_TOOL_SOCKET",
    "AGENT_COMMS_SELECTED_TOOL_TOKEN",
    "AGENT_COMMS_THREAD",
    "PI_PARENT_ID",
    "PI_SESSION_FILE",
    "PI_SESSION_ID",
    "PI_TASK",
    "PI_WORKTREE",
})


@pytest.fixture(autouse=True)
def _managed_runtime_environment_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's live managed process is not a test wire or native child."""
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
def delayed_launch(monkeypatch: pytest.MonkeyPatch):
    """Hold the first real exec verification; later launches run unchanged."""
    import asyncio
    import sys

    from agent_comms.child_process import Platform

    with Platform.current().launch((sys.executable, "-c", "pass"), ()) as launch:
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


@pytest.fixture
def comms(root: Path) -> Comms:
    return Comms(root)


@pytest.fixture
def wired(comms: Comms) -> Comms:
    """A wire with PR111 (parent) and fixer (child) registered."""
    comms.registry.declare(Thread(name="PR111", tags=frozenset({"base"}), worktree="/tmp/wt1"))
    comms.registry.declare(
        Thread(
            name="fixer",
            tags=frozenset({"auth"}),
            worktree="/tmp/wt1",
            parent="PR111",
            task="fix auth",
        )
    )
    return comms


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
        command, arguments, *, worktree, environment=None, session_file=None
    ):
        script = Path(command)
        if not script.is_file():
            return original(
                command,
                arguments,
                worktree=worktree,
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
                Path(worktree),
                Path(session_file) if session_file is not None else None,
                Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
            ),
            Path(worktree),
            runtime=LaunchRuntime.running(), configuration=RestartEnvironment.inherit(env),
        )

    def retained(
        launch, command, arguments, *, worktree, environment=None, session_file=None
    ):
        if (
            Path(command).is_file()
            and Path(command).resolve() == Path(launch.argv[0]).resolve()
            and Path(worktree).resolve() == launch.cwd.resolve()
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
            environment=environment,
            session_file=session_file,
        )

    monkeypatch.setattr(NativePiRpcLaunch, "managed", prepare)
    monkeypatch.setattr(NativePiRpcLaunch, "retained_managed", retained)
