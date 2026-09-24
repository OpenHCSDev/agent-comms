"""Real disposable bus/SQLite owner, with only the paid Pi boundary faked."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import select
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import cohort_foreground as foreground
from agent_comms import cohort_send
from agent_comms import coordinated_runtime as runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import (
    IdentityConflict,
    MutationStore,
    PublicationActivationBlocked,
)
from agent_comms.declarations import Thread, ThreadStatus
from agent_comms.native_pi import NativeContextProof, NativeTurnResult
from agent_comms.operations import Comms

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="foreground private roots require a real /var/tmp ancestry and Linux owner checks",
)


def _wire(base: Path) -> tuple[Path, str, Comms]:
    root = base / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    comms.register(Thread("sender", frozenset(), str(base), pid=os.getpid()))
    return root, comms.initialize_private_initial_protocol(), comms


def _fake_pi(calls: list[str]):
    async def run(
        package, *, input_id, prompt, worktree, session_dir, session_file=None, **_kwargs
    ):
        calls.append(input_id)
        assert session_file is None
        session_file = session_dir / "one.jsonl"
        entry_id = hashlib.sha256(input_id.encode()).hexdigest()[:16]
        session_file.write_text(
            json.dumps({"type": "session", "id": "foreground"})
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "id": entry_id,
                    "message": {
                        "role": "user",
                        "inputId": input_id,
                        "inputDigest": hashlib.sha256(prompt.encode()).hexdigest(),
                    },
                }
            )
            + "\n"
        )
        session_file.chmod(0o600)
        digest = hashlib.sha256(input_id.encode()).hexdigest()
        proof = Path(str(session_file) + ".input-proof")
        proof.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "type": "context_committed",
                    "sessionId": "foreground",
                    "inputId": input_id,
                    "sessionEntryId": entry_id,
                    "requestGeneration": 1,
                    "llmContextDigest": digest,
                }
            )
            + "\n"
        )
        proof.chmod(0o600)
        return NativeTurnResult(
            "42", NativeContextProof(input_id, "foreground", entry_id, 1, digest, session_file)
        )

    return run


async def test_foreground_registers_own_pid_and_seals_one_selected_direct(
    tmp_path: Path, monkeypatch
) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        calls: list[str] = []
        monkeypatch.setattr(foreground, "_trusted_package", lambda _: None)
        monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
        monkeypatch.setattr(runtime, "run_native_pi_turn", _fake_pi(calls))

        def ready(thread: Thread) -> None:
            assert thread.pid == os.getpid()
            assert comms.registry.require("beta").pid == os.getpid()
            assert comms.registry.status("beta") is ThreadStatus.RUNNING
            comms.send_initial_cohort("sender", "beta", "Compute 17+25")

        result = await foreground.run_foreground_once(
            root,
            wire_root_id=root_id,
            name="beta",
            worktree=base,
            tags=frozenset(),
            native_package=tmp_path,
            opt_in=True,
            wait_seconds=0,
            ready=ready,
        )
        assert result is not None and result.response_message_id
        assert result.exact_target == "sender" and len(calls) == 1
        assert comms.registry.status("beta") is ThreadStatus.STOPPED
        assert comms.dm_history("sender", "beta")[-1].body == "42"
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            assert (
                store._connection.execute("SELECT count(*) FROM native_runtime_inputs").fetchone()[
                    0
                ]
                == 1
            )


async def test_foreground_two_recipients_one_no_wake_and_no_model(
    tmp_path: Path, monkeypatch
) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        calls: list[str] = []
        monkeypatch.setattr(foreground, "_trusted_package", lambda _: None)
        monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
        monkeypatch.setattr(runtime, "run_native_pi_turn", _fake_pi(calls))
        ready_names: set[str] = set()

        def ready(thread: Thread) -> None:
            ready_names.add(thread.name)
            if len(ready_names) == 2:
                comms.send_initial_cohort("sender", "#team", "@beta Compute 17+25")

        alpha = asyncio.create_task(
            foreground.run_foreground_once(
                root,
                wire_root_id=root_id,
                name="alpha",
                worktree=base,
                tags=frozenset({"team"}),
                native_package=tmp_path,
                opt_in=True,
                wait_seconds=0.25,
                ready=ready,
            )
        )
        beta = asyncio.create_task(
            foreground.run_foreground_once(
                root,
                wire_root_id=root_id,
                name="beta",
                worktree=base,
                tags=frozenset({"team"}),
                native_package=tmp_path,
                opt_in=True,
                wait_seconds=0.25,
                ready=ready,
            )
        )
        alpha_result, beta_result = await asyncio.gather(alpha, beta)
        assert isinstance(alpha_result, foreground.NoWakeReceipt)
        assert beta_result is not None and beta_result.response_message_id
        assert len(calls) == 1
        assert comms.channel_history("#team")[-1].body == "42"
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            assert (
                store._connection.execute(
                    "SELECT count(*) FROM cohort_delivery_receipts "
                    "WHERE kind='unmentioned_observer'"
                ).fetchone()[0]
                == 1
            )
        assert comms.registry.status("alpha") is ThreadStatus.STOPPED
        assert comms.registry.status("beta") is ThreadStatus.STOPPED


async def test_foreground_refuses_takeover_and_cosmetic_subprocess_pid(
    tmp_path: Path, monkeypatch
) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        comms.register(Thread("beta", frozenset(), str(base), pid=os.getpid()))
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            install_private_response_schema(store)
            install_native_runtime_schema(store)
            store.register_participant(
                stable_thread_lookup(comms.registry.require("beta").created_at),
                "beta",
                "beta",
                committed=True,
            )
        initial = comms.send_initial_cohort("sender", "beta", "one message")
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            accept_initial_cohort(comms.bus, root_id, initial.seq, store)
        monkeypatch.setattr(foreground, "_trusted_package", lambda _: None)
        with pytest.raises(IdentityConflict, match="no takeover"):
            await foreground.run_foreground_once(
                root,
                wire_root_id=root_id,
                name="beta",
                worktree=base,
                tags=frozenset(),
                native_package=tmp_path,
                opt_in=True,
                wait_seconds=0,
            )
        # An ordinary subprocess cannot borrow the existing parent PID. Do
        # not launch a model: the registry fence fails before any reservation.
        code = """import asyncio, sys
from pathlib import Path
from agent_comms import coordinated_runtime as r
r._trusted_package = lambda _: None
try:
    asyncio.run(r.run_one_sealed_claim(Path(sys.argv[1]), wire_root_id=sys.argv[2],
        owner_name="beta", native_package=Path(sys.argv[1]), opt_in=True))
except Exception as error:
    print(type(error).__name__)
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(root), root_id],
            capture_output=True,
            text=True,
            timeout=12,
            check=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        assert child.stdout.strip() == "StaleFence"
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            assert (
                store._connection.execute("SELECT count(*) FROM native_runtime_inputs").fetchone()[
                    0
                ]
                == 0
            )
        assert comms.registry.status("beta") is ThreadStatus.RUNNING


def test_actual_foreground_command_owns_its_recipient_process(tmp_path: Path) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        # Only the paid Pi edge is mocked IN THE CHILD. The CLI, registry,
        # bus, SQLite acceptance/claim and publication run in its actual PID.
        script = """import sys
from agent_comms import cohort_foreground as f, coordinated_runtime as r
from test_cohort_foreground import _fake_pi
f._trusted_package = lambda _: None
r._trusted_package = lambda _: None
r.run_native_pi_turn = _fake_pi([])
raise SystemExit(f.main(sys.argv[1:]))
"""
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                "--root",
                str(root),
                "--wire-root-id",
                root_id,
                "--name",
                "beta",
                "--worktree",
                str(base),
                "--native-package",
                str(tmp_path),
                "--wait-seconds",
                "2",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(
                    (str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).parent))
                ),
            },
        )
        try:
            assert child.stdout is not None
            assert select.select([child.stdout], [], [], 8)[0], "no ready receipt"
            ready = json.loads(child.stdout.readline())
            assert ready == {"ready": True, "name": "beta", "pid": child.pid}
            assert comms.registry.require("beta").pid == child.pid
            sender = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.cohort_send",
                    "--root",
                    str(root),
                    "--wire-root-id",
                    root_id,
                    "--from",
                    "sender",
                    "--to",
                    "beta",
                    "--body",
                    "Compute 17+25",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=True,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            )
            assert json.loads(sender.stdout)["wire_seq"] == 1
            out, err = child.communicate(timeout=12)
            assert child.returncode == 0, (out, err)
            assert json.loads(out.strip())["response_message_id"]
            assert comms.registry.status("beta") is ThreadStatus.STOPPED
            assert comms.dm_history("sender", "beta")[-1].body == "42"
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_two_real_recipient_processes_emit_selected_and_typed_no_wake(tmp_path: Path) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        script = """import sys
from agent_comms import cohort_foreground as f, coordinated_runtime as r
from test_cohort_foreground import _fake_pi
f._trusted_package = lambda _: None
r._trusted_package = lambda _: None
r.run_native_pi_turn = _fake_pi([])
raise SystemExit(f.main(sys.argv[1:]))
"""
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                (str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).parent))
            ),
        }
        children: dict[str, subprocess.Popen[str]] = {}
        try:
            for name in ("alpha", "beta"):
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        "--opt-in",
                        "--root",
                        str(root),
                        "--wire-root-id",
                        root_id,
                        "--name",
                        name,
                        "--worktree",
                        str(base),
                        "--tags",
                        "team",
                        "--native-package",
                        str(tmp_path),
                        "--wait-seconds",
                        "1",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                children[name] = child
                assert child.stdout is not None
                assert select.select([child.stdout], [], [], 8)[0]
                assert json.loads(child.stdout.readline())["pid"] == child.pid
                assert comms.registry.require(name).pid == child.pid
            sender = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.cohort_send",
                    "--opt-in",
                    "--root",
                    str(root),
                    "--wire-root-id",
                    root_id,
                    "--from",
                    "sender",
                    "--to",
                    "#team",
                    "--body",
                    "@beta Compute 17+25",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=True,
                env=env,
            )
            assert json.loads(sender.stdout)["wire_seq"] == 1
            outcomes: dict[str, dict[str, object]] = {}
            for name, child in children.items():
                out, err = child.communicate(timeout=12)
                assert child.returncode == 0, (name, out, err)
                outcomes[name] = json.loads(out.strip())
            assert outcomes["alpha"] == {"disposition": "NO_WAKE", "wire_seq": 1}
            assert outcomes["beta"]["response_message_id"]
            assert comms.channel_history("#team")[-1].body == "42"
            with MutationStore(str(root / "coordination.sqlite3")) as store:
                assert (
                    store._connection.execute(
                        "SELECT count(*) FROM native_runtime_inputs"
                    ).fetchone()[0]
                    == 1
                )
        finally:
            for child in children.values():
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)


async def test_failed_model_reservation_is_not_polled_or_replayed(
    tmp_path: Path, monkeypatch
) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        calls: list[str] = []
        monkeypatch.setattr(foreground, "_trusted_package", lambda _: None)
        monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)

        async def uncertain(*args, input_id: str, **kwargs):
            calls.append(input_id)
            raise RuntimeError("model opportunity uncertain; manual disposition")

        monkeypatch.setattr(runtime, "run_native_pi_turn", uncertain)
        with pytest.raises(RuntimeError, match="manual disposition"):
            await foreground.run_foreground_once(
                root,
                wire_root_id=root_id,
                name="beta",
                worktree=base,
                tags=frozenset(),
                native_package=tmp_path,
                opt_in=True,
                wait_seconds=2,
                ready=lambda _: comms.send_initial_cohort("sender", "beta", "one message"),
            )
        assert len(calls) == 1
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            assert (
                store._connection.execute("SELECT count(*) FROM native_runtime_inputs").fetchone()[
                    0
                ]
                == 1
            )
        assert comms.registry.status("beta") is ThreadStatus.STOPPED
        with pytest.raises(IdentityConflict, match="no takeover"):
            await foreground.run_foreground_once(
                root,
                wire_root_id=root_id,
                name="beta",
                worktree=base,
                tags=frozenset(),
                native_package=tmp_path,
                opt_in=True,
                wait_seconds=0,
            )
        assert len(calls) == 1


def test_sender_is_enabled_on_an_initialized_private_root() -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        root, root_id, comms = _wire(Path(dirname))
        comms.register(Thread("beta", frozenset(), dirname, pid=os.getpid()))
        sequence, message_id = cohort_send.publish_one(
            root,
            wire_root_id=root_id,
            sender="sender",
            target="beta",
            body="private message",
        )
        assert sequence == 1
        assert message_id == comms.full_history()[0].message_id


def test_sender_requires_private_root_and_exact_marker(tmp_path: Path) -> None:
    with pytest.raises(PublicationActivationBlocked):
        cohort_send.publish_one(
            tmp_path,
            wire_root_id="x",
            sender="sender",
            target="beta",
            body="private message",
            opt_in=False,
        )
    assert not (tmp_path / "bus_meta.json").exists()
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        root, _, comms = _wire(Path(dirname))
        with pytest.raises(IdentityConflict, match="root changed"):
            cohort_send.publish_one(
                root,
                wire_root_id="wrong",
                sender="sender",
                target="beta",
                body="private message",
                opt_in=True,
            )
        assert comms.full_history() == []


def test_foreground_rejects_live_root_without_writing(tmp_path: Path) -> None:
    with pytest.raises(PublicationActivationBlocked):
        asyncio.run(
            foreground.run_foreground_once(
                tmp_path,
                wire_root_id="x",
                name="beta",
                worktree=tmp_path,
                tags=frozenset(),
                native_package=tmp_path,
                opt_in=False,
            )
        )
    assert not (tmp_path / "registry.json").exists()
