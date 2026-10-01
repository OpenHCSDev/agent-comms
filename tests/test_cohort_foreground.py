"""Real disposable bus/SQLite owner, with only the paid Pi boundary faked."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import cohort_foreground as foreground
from agent_comms import cohort_send
from agent_comms import coordinated_runtime as runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.errors import RelationViolationError
from agent_comms.native_pi import NativeContextProof, NativeTurnResult
from agent_comms.native_prompt_send import _enter_admission
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.private_sidecar import native_request_digest
from agent_comms.selected_actions import SelectedExistingFileWrite
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from native_proof_cases import write_proof_rows

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="foreground private roots require a real /var/tmp ancestry and Linux owner checks",
)


def _configured_thread(*args, **kwargs):
    """Offline foreground owners still have an explicit saved selection."""
    return Thread(*args, **kwargs, model="openai-codex/gpt-6-sol")


def _fake_package(package):
    """Supply the actual extension bytes; only the provider boundary is faked."""
    dist = Path(package) / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    extension = Path(__file__).parents[1] / "src/agent_comms/channel_coding_tools.mjs"
    (dist / extension.name).write_bytes(extension.read_bytes())


@pytest.fixture(autouse=True)
def configured_foreground(monkeypatch):
    monkeypatch.setattr(foreground, "Thread", _configured_thread)


def _wire(base: Path) -> tuple[Path, str, Comms]:
    root = base / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    comms.registry.declare(
        Thread(
            "sender", frozenset(), str(base), process_identity=ProcessIdentity.capture(os.getpid())
        )
    )
    return root, comms.messaging.initialize_private_initial_protocol(), comms


def _fake_pi(calls: list[str]):
    async def run(
        package, *, input_id, prompt, worktree, session_dir, session_file=None, **_kwargs
    ):
        assert session_file is None
        session_file = session_dir / "one.jsonl"
        # The real get_state provides an actual saved file before prompt send.
        session_file.write_text(json.dumps({"type": "session", "id": "foreground"}) + "\n")
        session_file.chmod(0o600)

        def admitted():
            # The fake Pi must cross the same irreversible send-admission
            # boundary before claiming a native context. Do not synthesize a
            # receipt from a reservation that was never sent.
            with _enter_admission(
                lambda: _kwargs["prompt_send_boundary"](session_file),
                threading.Event(),
                time.monotonic() + 5,
            ):
                calls.append(input_id)

        await asyncio.to_thread(admitted)
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
                        "inputDigest": native_request_digest(prompt),
                    },
                }
            )
            + "\n"
        )
        session_file.chmod(0o600)
        digest = hashlib.sha256(input_id.encode()).hexdigest()
        write_proof_rows(
            session_file,
            [
                {
                    "schema": 1,
                    "type": "context_committed",
                    "sessionId": "foreground",
                    "inputId": input_id,
                    "sessionEntryId": entry_id,
                    "requestGeneration": 1,
                    "llmContextDigest": digest,
                }
            ],
        )
        from agent_comms.pi_events import PiEvent

        observer = _kwargs["observe_event"]
        await observer(
            PiEvent.from_wire(
                {
                    "type": "response",
                    "id": "native-prompt",
                    "command": "prompt",
                    "success": True,
                }
            )
        )
        await observer(PiEvent.from_wire({"type": "context_committed", "inputId": input_id}))
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
        monkeypatch.setattr(foreground, "_trusted_package", _fake_package)
        monkeypatch.setattr(runtime, "_trusted_package", _fake_package)
        monkeypatch.setattr(TrackedTurnSession, "execute", _fake_pi(calls))

        def ready(thread: Thread) -> None:
            assert thread.pid == os.getpid()
            assert comms.registry.require("beta").pid == os.getpid()
            assert comms.registry.status("beta") == RunningThreadStatus()
            comms.messaging.send_initial_cohort("sender", "beta", "Compute 17+25")

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
        assert comms.registry.status("beta") == StoppedThreadStatus()
        assert comms.views.dm_history("sender", "beta")[-1].body == "42"
        with Coordination(str(root / "coordination.sqlite3")) as store:
            assert (
                store.session._connection.execute(
                    f"SELECT count(*) FROM {NativeRuntimeInput.declared_name}"
                ).fetchone()[0]
                == 1
            )


async def test_foreground_explicit_selected_existing_file_entry_mutates_under_claim(
    tmp_path: Path, monkeypatch
) -> None:
    with TemporaryDirectory(prefix="ac-selected-write-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        resource = base / "module.py"
        resource.write_bytes(b"before\n")
        root, root_id, comms = _wire(base)
        calls: list[str] = []
        monkeypatch.setattr(foreground, "_trusted_package", _fake_package)
        monkeypatch.setattr(runtime, "_trusted_package", _fake_package)
        monkeypatch.setattr(TrackedTurnSession, "execute", _fake_pi(calls))

        def ready(thread: Thread) -> None:
            assert resource.read_bytes() == b"before\n"
            comms.messaging.send_initial_cohort("sender", "beta", "Compute 17+25")

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
            selected_existing_file_write=SelectedExistingFileWrite(
                ExistingFileClaim(Path(resource)), b"after selected claim\n"
            ),
        )
        assert result is not None and result.response_message_id
        assert len(calls) == 1 and resource.read_bytes() == b"after selected claim\n"
        claimed = Comms(root).bus.log.claim_projection()[str(resource)]
        assert (
            claimed.admission is not None
            and claimed.admission.wake_assignment_id == result.assignment_ids[0]
        )
        assert comms.views.dm_history("sender", "beta")[-1].body == "42"


async def test_foreground_selected_write_preflight_refuses_uninitialized_or_external_resource(
    tmp_path: Path, monkeypatch
) -> None:
    package = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
    if not package:
        pytest.skip("Requires actual prepared native package for reviewed layout preflight")
    with TemporaryDirectory(prefix="ac-selected-preflight-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        resource = base / "module.py"
        resource.write_bytes(b"before\n")
        root = base / "wire"
        root.mkdir(mode=0o700)
        comms = Comms(root, private_initial_writes=True)
        root_id = "0" * 32  # No issuer has initialized this root yet.
        plan = SelectedExistingFileWrite(ExistingFileClaim(Path(resource)), b"forbidden\n")
        with pytest.raises(RelationViolationError, match="no durable protocol marker"):
            await foreground.run_foreground_once(
                root,
                wire_root_id=root_id,
                name="alpha",
                worktree=base,
                tags=frozenset(),
                native_package=Path(package),
                wait_seconds=0,
                selected_existing_file_write=plan,
            )
        assert "alpha" not in comms.registry and resource.read_bytes() == b"before\n"
        root_id = comms.messaging.initialize_private_initial_protocol()
        external = tmp_path / "external.py"
        external.write_bytes(b"external\n")
        with pytest.raises(ValueError, match="inside the worktree"):
            await foreground.run_foreground_once(
                root,
                wire_root_id=root_id,
                name="alpha",
                worktree=base,
                tags=frozenset(),
                native_package=Path(package),
                wait_seconds=0,
                selected_existing_file_write=SelectedExistingFileWrite(
                    ExistingFileClaim(Path(external)), b"forbidden\n"
                ),
            )
        assert "alpha" not in comms.registry and external.read_bytes() == b"external\n"


def test_foreground_cli_passes_bounded_source_to_explicit_selected_write_entry(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    with TemporaryDirectory(prefix="ac-selected-cli-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        resource = base / "module.py"
        source = base / "replacement.txt"
        resource.write_bytes(b"before\n")
        source.write_bytes(b"operator bytes\n")
        root, root_id, _comms = _wire(base)
        observed = []
        monkeypatch.setattr(foreground, "_trusted_package", _fake_package)

        async def capture(*args, **kwargs):
            observed.append(kwargs["selected_existing_file_write"])
            return None

        monkeypatch.setattr(foreground, "run_foreground_once", capture)
        argv = [
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
            "--selected-write-resource",
            str(resource),
            "--selected-write-source",
            str(source),
        ]
        assert foreground.main(argv) == 0
        assert observed == [
            SelectedExistingFileWrite(ExistingFileClaim(Path(resource)), b"operator bytes\n")
        ]
        assert resource.read_bytes() == b"before\n"  # Parser alone never writes.
        assert "NO_SELECTED_CLAIM" in capsys.readouterr().out
        observed.clear()
        alias = base / "source-alias"
        alias.symlink_to(source)
        argv[-1] = str(alias)
        assert foreground.main(argv) == 1
        assert observed == []
        fifo = base / "source-fifo"
        os.mkfifo(fifo)
        # A blocking open of a reader-only FIFO must not hold the owner entry
        # indefinitely before its regular-file check or wait deadline.
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; "
                "from agent_comms.cohort_foreground import _read_selected_write_source; "
                "\ntry: _read_selected_write_source(Path(sys.argv[1]))"
                "\nexcept ValueError: raise SystemExit(0)"
                "\nraise SystemExit(1)",
                str(fifo),
            ],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        assert probe.returncode == 0, probe.stderr


async def test_foreground_explicit_selected_write_never_mutates_no_wake(
    tmp_path: Path, monkeypatch
) -> None:
    with TemporaryDirectory(prefix="ac-selected-no-wake-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        resource = base / "module.py"
        resource.write_bytes(b"unchanged\n")
        root, root_id, comms = _wire(base)
        comms.registry.declare(
            Thread(
                "beta",
                frozenset({"team"}),
                str(base),
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
        calls: list[str] = []
        monkeypatch.setattr(foreground, "_trusted_package", _fake_package)
        monkeypatch.setattr(runtime, "_trusted_package", _fake_package)
        monkeypatch.setattr(TrackedTurnSession, "execute", _fake_pi(calls))

        def ready(thread: Thread) -> None:
            with Coordination(str(root / "coordination.sqlite3")) as store:
                store.participants.register(
                    stable_thread_lookup(comms.registry.require("beta").created_at),
                    "beta",
                    "beta",
                    committed=True,
                )
            comms.messaging.send_initial_cohort("sender", "#team", "@beta only")

        result = await foreground.run_foreground_once(
            root,
            wire_root_id=root_id,
            name="alpha",
            worktree=base,
            tags=frozenset({"team"}),
            native_package=tmp_path,
            wait_seconds=0,
            ready=ready,
            selected_existing_file_write=SelectedExistingFileWrite(
                ExistingFileClaim(Path(resource)), b"forbidden\n"
            ),
        )
        assert isinstance(result, foreground.NoWakeReceipt)
        assert calls == [] and resource.read_bytes() == b"unchanged\n"
        assert Comms(root).bus.log.claim_projection().get(str(resource)) is None


async def test_foreground_two_recipients_one_no_wake_and_no_model(
    tmp_path: Path, monkeypatch
) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        calls: list[str] = []
        monkeypatch.setattr(foreground, "_trusted_package", _fake_package)
        monkeypatch.setattr(runtime, "_trusted_package", _fake_package)
        monkeypatch.setattr(TrackedTurnSession, "execute", _fake_pi(calls))
        ready_names: set[str] = set()

        def ready(thread: Thread) -> None:
            ready_names.add(thread.name)
            if len(ready_names) == 2:
                comms.messaging.send_initial_cohort("sender", "#team", "@beta Compute 17+25")

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
        assert comms.views.channel_history("#team")[-1].body == "42"
        with Coordination(str(root / "coordination.sqlite3")) as store:
            assert (
                store.session._connection.execute(
                    "SELECT count(*) FROM cohort_delivery_receipts "
                    "WHERE kind='unmentioned_observer'"
                ).fetchone()[0]
                == 1
            )
        assert comms.registry.status("alpha") == StoppedThreadStatus()
        assert comms.registry.status("beta") == StoppedThreadStatus()


async def test_foreground_refuses_takeover_and_cosmetic_subprocess_pid(
    tmp_path: Path, monkeypatch
) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        comms.registry.declare(
            Thread(
                "beta",
                frozenset(),
                str(base),
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
        with Coordination(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            install_private_response_schema(store)
            install_native_runtime_schema(store)
            store.participants.register(
                stable_thread_lookup(comms.registry.require("beta").created_at),
                "beta",
                "beta",
                committed=True,
            )
        initial = comms.messaging.send_initial_cohort("sender", "beta", "one message")
        with Coordination(str(root / "coordination.sqlite3")) as store:
            accept_delivery_cohort(comms.bus, root_id, initial.seq, store)
        monkeypatch.setattr(foreground, "_trusted_package", _fake_package)
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
    asyncio.run(r.SelectedExecution(root=Path(sys.argv[1]), wire_root_id=sys.argv[2],
        owner_name="beta", native_package=Path(sys.argv[1]), opt_in=True).run())
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
        with Coordination(str(root / "coordination.sqlite3")) as store:
            assert (
                store.session._connection.execute(
                    f"SELECT count(*) FROM {NativeRuntimeInput.declared_name}"
                ).fetchone()[0]
                == 0
            )
        assert comms.registry.status("beta") == RunningThreadStatus()


def test_actual_foreground_command_owns_its_recipient_process(tmp_path: Path) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        # Only the paid Pi edge is mocked IN THE CHILD. The CLI, registry,
        # bus, SQLite acceptance/claim and publication run in its actual PID.
        script = """import sys
import threading
import time
from agent_comms import cohort_foreground as f, coordinated_runtime as r
from test_cohort_foreground import _fake_pi, _fake_package, _configured_thread
f.Thread = _configured_thread
f._trusted_package = _fake_package
r._trusted_package = _fake_package
from agent_comms.tracked_turn import TrackedTurnSession
TrackedTurnSession.execute = _fake_pi([])
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
            assert comms.registry.status("beta") == StoppedThreadStatus()
            assert comms.views.dm_history("sender", "beta")[-1].body == "42"
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_two_real_recipient_processes_emit_selected_and_typed_no_wake(tmp_path: Path) -> None:
    with TemporaryDirectory(prefix="ac-foreground-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root, root_id, comms = _wire(base)
        script = r"""import sys
import threading
import time
from agent_comms import cohort_foreground as f, coordinated_runtime as r
from test_cohort_foreground import _fake_pi, _fake_package, _configured_thread
f.Thread = _configured_thread
f._trusted_package = _fake_package
r._trusted_package = _fake_package
from agent_comms.tracked_turn import TrackedTurnSession
TrackedTurnSession.execute = _fake_pi([])
original_run = f.run_foreground_once
async def wait_for_committed_source(*args, ready=None, **kwargs):
    def registered(thread):
        ready(thread)
        if sys.stdin.readline() != "committed\n":
            raise RuntimeError("parent did not release committed-source barrier")
    try:
        return await original_run(*args, ready=registered, **kwargs)
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
f.run_foreground_once = wait_for_committed_source
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
                    stdin=subprocess.PIPE,
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
            # Both frozen recipients exist at commit. Finish alpha's original
            # passive observation before beta publishes a NEW reply: that reply
            # correctly gives alpha a triage wake, covered by the native roundtrip
            # test rather than this original-cohort NO_WAKE fixture.
            outcomes: dict[str, dict[str, object]] = {}
            for name, child in children.items():
                assert child.stdin is not None
                child.stdin.write("committed\n")
                child.stdin.flush()
                child.stdin.close()
                child.stdin = None
                out, err = child.communicate(timeout=12)
                assert child.returncode == 0, f"{name}: {out}\n{err}"
                outcomes[name] = json.loads(out.strip())
            assert outcomes["alpha"] == {"disposition": "NO_WAKE", "wire_seq": 1}
            assert outcomes["beta"]["response_message_id"]
            assert comms.views.channel_history("#team")[-1].body == "42"
            with Coordination(str(root / "coordination.sqlite3")) as store:
                assert (
                    store.session._connection.execute(
                        f"SELECT count(*) FROM {NativeRuntimeInput.declared_name}"
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
        monkeypatch.setattr(foreground, "_trusted_package", _fake_package)
        monkeypatch.setattr(runtime, "_trusted_package", _fake_package)

        async def uncertain(*args, input_id: str, **kwargs):
            calls.append(input_id)
            raise RuntimeError("model opportunity uncertain; manual disposition")

        monkeypatch.setattr(TrackedTurnSession, "execute", uncertain)
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
                ready=lambda _: comms.messaging.send_initial_cohort(
                    "sender", "beta", "one message"
                ),
            )
        assert len(calls) == 1
        with Coordination(str(root / "coordination.sqlite3")) as store:
            assert (
                store.session._connection.execute(
                    f"SELECT count(*) FROM {NativeRuntimeInput.declared_name}"
                ).fetchone()[0]
                == 1
            )
        assert comms.registry.status("beta") == StoppedThreadStatus()
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
        comms.registry.declare(
            Thread(
                "beta", frozenset(), dirname, process_identity=ProcessIdentity.capture(os.getpid())
            )
        )
        sequence, message_id = cohort_send.publish_one(
            root,
            wire_root_id=root_id,
            sender="sender",
            target="beta",
            body="private message",
        )
        assert sequence == 1
        assert message_id == comms.views.full_history()[0].message_id


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
        assert comms.views.full_history() == []


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
