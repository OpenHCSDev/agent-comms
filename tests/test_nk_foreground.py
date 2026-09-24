"""Fresh private foreground owner protocol; model boundary is strictly offline fake.

These tests do not prove a provider spend ceiling, copied-Pi trust or release.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms import nk_foreground as foreground
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import MutationStore, PublicationActivationBlocked
from agent_comms.declarations import RelationViolationError, Thread
from agent_comms.nk_foreground import reserve_foreground_owner
from agent_comms.operations import Comms
from test_coordinated_runtime import _fake_model

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="private foreground N/K requires Linux /var/tmp and fork"
)


@pytest.fixture
def tmp_path():
    # Private N/K publication deliberately rejects /tmp, even when pytest's
    # default basetemp is there. Keep the real durability fixture on /var/tmp.
    with tempfile.TemporaryDirectory(prefix="ac-nk-foreground-", dir="/var/tmp") as directory:
        yield Path(directory)


def _private_root(tmp_path: Path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    (root / "work").mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    comms.register(Thread("sender", frozenset(), str(root / "work"), pid=os.getpid()))
    return root, comms, comms.initialize_private_initial_protocol()


def _recipient(pipe, root: Path, root_id: str, name: str, decision: str = "FULL") -> None:
    """Actual OS recipient: own PID registers, waits GO, executes one claim."""
    from agent_comms import coordinated_runtime as runtime
    from agent_comms import nk_foreground as foreground

    model, calls = _fake_model(decision=decision)
    with (
        patch.object(foreground, "_trusted_package", return_value=None),
        patch.object(runtime, "_trusted_package", return_value=None),
        patch.object(runtime, "run_native_pi_turn", model),
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
                    result.disposition.value if result else None,
                    result.exact_target if result else None,
                    result.response_message_id if result else None,
                    len(calls),
                )
            )
        except Exception as error:
            pipe.send(("error", type(error).__name__))
        finally:
            pipe.close()


def _accept(root: Path, root_id: str, comms: Comms, message) -> None:
    initial = comms.bus.read_initial_cohort(root_id, message.seq)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        for recipient in initial.audience.recipients:
            store.register_participant(
                recipient.recipient_lookup,
                recipient.canonical_thread,
                recipient.canonical_thread,
                committed=True,
            )
        assert accept_initial_cohort(comms.bus, root_id, message.seq, store).value.member_count == 2


def test_actual_foreground_pid_n2_k1_and_duplicate_owner_denied(tmp_path: Path) -> None:
    root, comms, root_id = _private_root(tmp_path)
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

        message = comms.send_initial_cohort("sender", "#team", "@beta Compute 17+25.")
        initial = comms.bus.read_initial_cohort(root_id, message.seq)
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
        assert result[0:3] == ("terminal", "completed", "#team")
        assert result[3] and result[4] == 1
        for child in children:
            child.join(6)
            assert child.exitcode == 0
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            assert (
                store._connection.execute(
                    "SELECT count(*) FROM cohort_delivery_receipts "
                    "WHERE kind='unmentioned_observer'"
                ).fetchone()[0]
                == 1
            )
            assert (
                store._connection.execute("SELECT count(*) FROM native_runtime_inputs").fetchone()[
                    0
                ]
                == 1
            )
        assert comms.channel_history("#team")[-1].sender == "beta"
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


def test_uncertain_model_attempt_is_never_replayed_by_new_foreground_owner(tmp_path: Path) -> None:
    root, comms, root_id = _private_root(tmp_path)
    with (
        patch("agent_comms.nk_foreground._trusted_package", return_value=None),
        patch("agent_comms.coordinated_runtime._trusted_package", return_value=None),
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
        comms.register(Thread("alpha", frozenset({"team"}), str(root / "work"), pid=os.getpid()))
        message = comms.send_initial_cohort("sender", "#team", "@beta Compute 17+25.")
        _accept(root, root_id, comms, message)
        failing, attempts = _fake_model(fail_on=1)
        with (
            patch("agent_comms.coordinated_runtime.run_native_pi_turn", failing),
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
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            assert (
                store._connection.execute("SELECT count(*) FROM native_runtime_inputs").fetchone()[
                    0
                ]
                == 1
            )
        assert not (root / "read_markers.json").exists()


def test_cli_main_ready_then_single_go_offline_model_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, comms, root_id = _private_root(tmp_path)
    comms.register(Thread("alpha", frozenset({"team"}), str(root / "work"), pid=os.getpid()))
    fake, calls = _fake_model()
    monkeypatch.setattr(foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", fake)
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
            message = comms.send_initial_cohort("sender", "#team", "@beta Compute 17+25.")
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
                "--private-opt-in",
                "--native-model-opt-in",
            ]
        )
        == 0
    )
    assert [event["status"] for event in events] == ["ready", "terminal"]
    assert events[1]["disposition"] == "completed"
    assert len(calls) == 1
    assert commands == [30]  # A second GO is never read by this process.


def test_explicit_stop_is_not_falsely_reported_as_registry_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, comms, root_id = _private_root(tmp_path)
    monkeypatch.setattr(foreground, "_trusted_package", lambda _: None)
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
    assert not (root / "coordination.sqlite3").exists()


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


def test_no_implicit_root_or_optin_and_no_owner_replacement(tmp_path: Path) -> None:
    root, comms, root_id = _private_root(tmp_path)
    with patch("agent_comms.nk_foreground._trusted_package", return_value=None):
        with pytest.raises(PublicationActivationBlocked):
            reserve_foreground_owner(
                root,
                wire_root_id=root_id,
                name="beta",
                task="task",
                tags=frozenset({"team"}),
                native_package=root / "fake-pi",
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
            opt_in=True,
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
