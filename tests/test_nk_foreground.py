"""Fresh private foreground owner protocol; model boundary is strictly offline fake.

These tests do not prove a provider spend ceiling, copied-Pi trust or release.
"""

from __future__ import annotations

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


def _fake_package(package):
    """Supply the actual extension bytes; only the provider boundary is faked."""
    dist = Path(package) / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    extension = Path(__file__).parents[1] / "src/agent_comms/channel_coding_tools.mjs"
    (dist / extension.name).write_bytes(extension.read_bytes())


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
    with (
        patch.object(foreground, "_trusted_package", side_effect=_fake_package),
        patch("agent_comms.native_pi._trusted_package", side_effect=_fake_package),
        patch.object(TrackedTurnSession, "execute", model),
    ):
        try:
            owner = reserve_foreground_owner(
                root,
                wire_root_id=root_id,
                name=name,
                task="arithmetic owner" if name == "beta" else "documentation owner",
                tags=frozenset({"team"}),
                native_package=root / "fake-pi",
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


def test_actual_foreground_pid_n2_k1_and_duplicate_owner_denied(private_comms, sender) -> None:
    comms, root = private_comms, private_comms.root
    root_id = comms.bus.log.read_metadata_unlocked().wire_root_id
    ctx = multiprocessing.get_context("fork")
    alpha_parent, alpha_child = ctx.Pipe()
    beta_parent, beta_child = ctx.Pipe()
    alpha = ctx.Process(target=_recipient, args=(alpha_child, root, root_id, "alpha"))
    beta = ctx.Process(target=_recipient, args=(beta_child, root, root_id, "beta"))
    children = (alpha, beta)
    duplicate: multiprocessing.Process | None = None
    duplicate_parent = None
    try:
        for child in children:
            child.start()
        assert alpha_parent.poll(6) and beta_parent.poll(6)
        assert alpha_parent.recv()[0] == "ready"
        ready = beta_parent.recv()
        assert ready[0] == "ready"
        # Registry's own PID check is intentionally callable only by beta.
        assert comms.registry.require("beta").pid == ready[1]
        duplicate_parent, duplicate_child = ctx.Pipe()
        duplicate = ctx.Process(target=_recipient, args=(duplicate_child, root, root_id, "beta"))
        duplicate.start()
        assert duplicate_parent.poll(6)
        assert duplicate_parent.recv() == ("error", "RelationViolationError")
        duplicate.join(6)
        assert duplicate.exitcode == 0
        assert comms.registry.require("beta").pid == ready[1]

        message = publish_initial(comms, "sender", "#team", "@beta Compute 17+25.")
        initial = comms.bus.log.read_delivery_cohort(root_id, message.seq)
        assert len(initial.audience.recipients) == 2
        assert (
            sum(decision.__class__.__name__ == "NoWakeDecision" for decision in initial.decisions)
            == 1
        )
        _accept(root, root_id, comms, message)
        alpha_parent.send("GO 0")
        beta_parent.send("GO 0")
        assert alpha_parent.poll(12) and beta_parent.poll(12)
        assert alpha_parent.recv() == ("terminal", None, None, None, 0)
        result = beta_parent.recv()
        assert result[0:3] == ("terminal", "completed", ("#team",))
        assert result[3] and result[4] == 1
        for child in children:
            child.join(6)
            assert child.exitcode == 0
        with Coordination(str(root / "coordination.sqlite3")) as store:
            assert (
                store.session._connection.execute(
                    "SELECT count(*) FROM cohort_delivery_receipts "
                    "WHERE kind='unmentioned_observer'"
                ).fetchone()[0]
                == 1
            )
            assert (
                store.session._connection.execute(
                    f"SELECT count(*) FROM {NativeRuntimeInput.declared_name}"
                ).fetchone()[0]
                == 1
            )
        assert list(comms.views.channel_history_page("#team").messages)[-1].sender == "beta"
        assert not (root / "read_markers.json").exists()
    finally:
        for child in (*children, duplicate):
            if child is not None and child.is_alive():
                child.terminate()
                child.join(6)
        alpha_parent.close()
        beta_parent.close()
        if duplicate_parent is not None:
            duplicate_parent.close()


def test_uncertain_model_attempt_is_never_replayed_by_new_foreground_owner(private_comms, sender) -> None:
    comms, root = private_comms, private_comms.root
    root_id = comms.bus.log.read_metadata_unlocked().wire_root_id
    with (
        patch("agent_comms.nk_foreground._trusted_package", side_effect=_fake_package),
        patch("agent_comms.native_pi._trusted_package", side_effect=_fake_package),
    ):
        owner = reserve_foreground_owner(
            root,
            wire_root_id=root_id,
            name="beta",
            task="arithmetic owner",
            tags=frozenset({"team"}),
            native_package=root / "fake-pi",
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
                native_package=root / "fake-pi",
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


def test_cli_main_ready_then_single_go_offline_model_boundary(
    private_comms, sender, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, root = private_comms, private_comms.root
    root_id = comms.bus.log.read_metadata_unlocked().wire_root_id
    comms.registry.declare(
        comms.registry.new_thread('alpha', frozenset({'team'}), str(root / 'work'), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    fake, calls = _fake_model()
    monkeypatch.setattr(foreground, "_trusted_package", _fake_package)
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", _fake_package)
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", fake)
    commands: list[int] = []

    def explicit_go(timeout: int) -> str:
        commands.append(timeout)
        return "GO 0"

    monkeypatch.setattr(foreground, "_read_go_command", explicit_go)
    events: list[dict[str, object]] = []

    def emit(payload: dict[str, object]) -> None:
        events.append(payload)
        if payload.get("status") == "ready":
            assert comms.registry.require("beta").pid == os.getpid()
            message = publish_initial(comms, "sender", "#team", "@beta Compute 17+25.")
            _accept(root, root_id, comms, message)

    monkeypatch.setattr(foreground, "_emit", emit)
    assert (
        foreground.main(
            [
                "--root",
                str(root),
                "--wire-root-id",
                root_id,
                "--name",
                "beta",
                "--task",
                "arithmetic owner",
                "--tags",
                "team",
                "--native-package",
                str(root / "fake-pi"),
            ]
        )
        == 0
    )
    assert [event["status"] for event in events] == ["ready", "terminal"]
    assert events[1]["disposition"] == "completed"
    assert len(calls) == 1
    assert commands == [30]  # A second GO is never read by this process.


def test_explicit_stop_is_not_falsely_reported_as_registry_stopped(
    private_comms, sender, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, root = private_comms, private_comms.root
    root_id = comms.bus.log.read_metadata_unlocked().wire_root_id
    monkeypatch.setattr(foreground, "_trusted_package", _fake_package)
    monkeypatch.setattr(foreground, "_read_go_command", lambda _timeout: "STOP")
    events: list[dict[str, object]] = []
    monkeypatch.setattr(foreground, "_emit", events.append)
    assert (
        foreground.main(
            [
                "--root",
                str(root),
                "--wire-root-id",
                root_id,
                "--name",
                "beta",
                "--task",
                "arithmetic owner",
                "--tags",
                "team",
                "--native-package",
                str(root / "fake-pi"),
                "--private-opt-in",
                "--native-model-opt-in",
            ]
        )
        == 0
    )
    assert [row["status"] for row in events] == ["ready", "go_declined"]
    assert events[-1]["registration"] == "retained_for_manual_disposition"
    assert comms.registry.require("beta").pid == os.getpid()
    # Registration now initializes the canonical store. STOP must still leave
    # it with no selected execution or attempt, rather than requiring no file.
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert store.session._connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        assert store.session._connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


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


def test_private_root_marker_and_no_owner_replacement(private_comms, sender) -> None:
    comms, root = private_comms, private_comms.root
    root_id = comms.bus.log.read_metadata_unlocked().wire_root_id
    with patch("agent_comms.nk_foreground._trusted_package", side_effect=_fake_package):
        with pytest.raises(PublicationActivationBlocked):
            reserve_foreground_owner(
                root,
                wire_root_id=root_id,
                name="beta",
                task="task",
                tags=frozenset({"team"}),
                native_package=root / "fake-pi",
                opt_in=False,
            )
        with pytest.raises(RelationViolationError):
            reserve_foreground_owner(
                root,
                wire_root_id="wrong",
                name="beta",
                task="task",
                tags=frozenset({"team"}),
                native_package=root / "fake-pi",
                opt_in=True,
            )
        assert "beta" not in comms.registry.all_threads()
        first = reserve_foreground_owner(
            root,
            wire_root_id=root_id,
            name="beta",
            task="task",
            tags=frozenset({"team"}),
            native_package=root / "fake-pi",
        )
        assert first.name == "beta"
        with pytest.raises(RelationViolationError, match="already exists"):
            reserve_foreground_owner(
                root,
                wire_root_id=root_id,
                name="beta",
                task="replacement",
                tags=frozenset({"team"}),
                native_package=root / "fake-pi",
                opt_in=True,
            )
        assert comms.registry.require("beta").task == "task"
        with pytest.raises(ValueError, match="Expected one GO"):
            asyncio.run(first.run_go("GO 0\n"))
