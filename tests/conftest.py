"""Shared fixtures: an isolated Comms wire per test."""

from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.threads import Thread


@pytest.fixture(autouse=True)
def _deferred_candidate_scheduler_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep optional background WAL workers out of unrelated test temp cleanup.

    tests/test_candidate_maintenance.py opts back in for the production hook.
    This does not affect fresh subprocess processes used by integration tests.
    """
    monkeypatch.setattr(
        "agent_comms.messaging.schedule_candidate_catchup", lambda *_: None
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
    comms.threads.register(Thread(name="PR111", tags=frozenset({"base"}), worktree="/tmp/wt1"))
    comms.threads.register(
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

    from agent_comms.native_pi import NativePiRpcLaunch

    original = NativePiRpcLaunch.managed

    def prepare(
        command, arguments, *, worktree, environment=None, session_file=None, fork_session=False
    ):
        script = Path(command)
        if not script.is_file():
            return original(
                command,
                arguments,
                worktree=worktree,
                environment=environment,
                session_file=session_file,
                fork_session=fork_session,
            )
        args = NativePiRpcLaunch.rpc_arguments(arguments)
        if session_file:
            args += ("--fork" if fork_session else "--session", session_file)
        env = dict(os.environ)
        env.update(environment or {})
        return NativePiRpcLaunch(
            (command, *args),
            Path(worktree),
            env,
            Path(worktree),
            Path(session_file) if session_file else None,
            Path(worktree),
        )

    monkeypatch.setattr(NativePiRpcLaunch, "managed", prepare)
