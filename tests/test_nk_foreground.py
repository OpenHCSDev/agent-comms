"""Fresh private foreground owner protocol; model boundary is strictly offline fake.

These tests do not prove a provider spend ceiling, copied-Pi trust or release.
"""

from __future__ import annotations

from bus_history import read_history
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from conftest import publish_initial
import asyncio
import multiprocessing
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import agent_comms.nk_foreground as foreground
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import PublicationActivationBlocked
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.nk_foreground import reserve_foreground_owner
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from test_coordinated_runtime import _fake_model

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="private foreground N/K requires Linux /var/tmp and fork"
)





@pytest.fixture(autouse=True)
def configured_foreground(monkeypatch):
    from agent_comms.registration import Registration
    create = Registration.new_thread

    def configured(*args, **kwargs):
        return create(*args, **kwargs, model="openai-codex/gpt-6-sol")

    monkeypatch.setattr(Registration, "new_thread", configured)


@pytest.fixture
def sender(private_comms) -> Path:
    """The publishing participant and the shared worktree under the private root."""
    work = private_comms.root / "work"
    work.mkdir(mode=0o700)
    private_comms.registry.declare(
        private_comms.registry.new_thread('sender', frozenset(), str(work), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    return work


def _recipient(pipe, root: Path, root_id: str, name: str, decision: str = "FULL") -> None:
    """Actual OS recipient: own PID registers, waits GO, executes one claim."""
    import agent_comms.coordinated_runtime as runtime
    import agent_comms.nk_foreground as foreground

    model, calls = _fake_model(decision=decision)
    with patch.object(TrackedTurnSession, 'execute', model):
        try:
            owner = reserve_foreground_owner(
                root,
                wire_root_id=root_id,
                name=name,
                task="arithmetic owner" if name == "beta" else "documentation owner",
                tags=frozenset({"team"}),
                native_package=VerifiedNativePackage.acquire(pinned_native_package()),
                opt_in=True,
            )
            pipe.send(("ready", os.getpid()))
            command = pipe.recv()
            result = asyncio.run(owner.run_go(command))
            pipe.send(
                (
                    "terminal",
                    result.disposition.declared_name if result else None,
                    tuple(acknowledgement.exact_target for acknowledgement in result.publications) if result else None,
                    tuple(acknowledgement.message_id for acknowledgement in result.publications) if result else None,
                    len(calls),
                )
            )
        except Exception as error:
            pipe.send(("error", type(error).__name__))
        finally:
            pipe.close()


def _accept(root: Path, root_id: str, comms: Comms, message) -> None:
    initial = comms.bus.log.read_delivery_cohort(root_id, message.seq)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        for recipient in initial.audience.recipients:
            store.participants.register(
                recipient.recipient_lookup,
                recipient.canonical_thread,
                recipient.canonical_thread,
                committed=True,
            )
        assert accept_delivery_cohort(comms.bus, root_id, message.seq, store).value.member_count == 2


def test_uncertain_model_attempt_is_never_replayed_by_new_foreground_owner(private_comms, sender, *, native_runtime_package) -> None:
    comms, root = private_comms, private_comms.root
    root_id = comms.bus.log.read_metadata_unlocked().wire_root_id
    owner = reserve_foreground_owner(
        root,
        wire_root_id=root_id,
        name="beta",
        task="arithmetic owner",
        tags=frozenset({"team"}),
        native_package=native_runtime_package,
        opt_in=True,
    )
    comms.registry.declare(
        comms.registry.new_thread('alpha', frozenset({'team'}), str(root / 'work'), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    message = publish_initial(comms, "sender", "#team", "@beta Compute 17+25.")
    _accept(root, root_id, comms, message)
    failing, attempts = _fake_model(fail_on=1)
    with (
        patch("agent_comms.tracked_turn.TrackedTurnSession.execute", failing),
        pytest.raises(RuntimeError),
    ):
        asyncio.run(owner.run_go("GO 0"))
    assert len(attempts) == 1
    with pytest.raises(PublicationActivationBlocked, match="exactly one GO"):
        asyncio.run(owner.run_go("GO 0"))
    assert len(attempts) == 1
    with pytest.raises(RelationViolationError, match="already exists"):
        reserve_foreground_owner(
            root,
            wire_root_id=root_id,
            name="beta",
            task="arithmetic owner",
            tags=frozenset({"team"}),
            native_package=native_runtime_package,
            opt_in=True,
        )
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                f"SELECT count(*) FROM {NativeRuntimeInput.declared_name}"
            ).fetchone()[0]
            == 1
        )
    assert not (root / "read_markers.json").exists()


def test_partial_go_frame_times_out_without_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"GO 0")  # A peer keeps the pipe open but never sends newline.
        with os.fdopen(os.dup(read_fd), "rb", buffering=0) as source:
            monkeypatch.setattr("sys.stdin", source)
            with pytest.raises(TimeoutError, match="did not complete"):
                foreground._read_go_command(1)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_private_root_marker_and_no_owner_replacement(private_comms, sender, *, native_runtime_package) -> None:
    comms, root = private_comms, private_comms.root
    root_id = comms.bus.log.read_metadata_unlocked().wire_root_id
    with pytest.raises(PublicationActivationBlocked):
        reserve_foreground_owner(
            root,
            wire_root_id=root_id,
            name="beta",
            task="task",
            tags=frozenset({"team"}),
            native_package=native_runtime_package,
            opt_in=False,
        )
    with pytest.raises(RelationViolationError):
        reserve_foreground_owner(
            root,
            wire_root_id="wrong",
            name="beta",
            task="task",
            tags=frozenset({"team"}),
            native_package=native_runtime_package,
            opt_in=True,
        )
    assert "beta" not in comms.registry.all_threads()
    first = reserve_foreground_owner(
        root,
        wire_root_id=root_id,
        name="beta",
        task="task",
        tags=frozenset({"team"}),
        native_package=native_runtime_package,
    )
    assert first.name == "beta"
    with pytest.raises(RelationViolationError, match="already exists"):
        reserve_foreground_owner(
            root,
            wire_root_id=root_id,
            name="beta",
            task="replacement",
            tags=frozenset({"team"}),
            native_package=native_runtime_package,
            opt_in=True,
        )
    assert comms.registry.require("beta").task == "task"
    with pytest.raises(ValueError, match="Expected one GO"):
        asyncio.run(first.run_go("GO 0\n"))
