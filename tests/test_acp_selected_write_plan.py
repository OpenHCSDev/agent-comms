"""Default-OFF public ACP operator preplan; selected file bytes are not model tools."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import cohort_foreground, coordinated_runtime
from agent_comms.acp import CommsAgent
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_foreground import _accept_visible_initials
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import sealed_cohort_claims
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import IdentityConflict, MutationStore
from agent_comms.declarations import MessageBus, Thread
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_prompt_binding import install_prompt_binding_schema
from agent_comms.operations import Comms
from agent_comms.selected_write_plan import SelectedWritePlans
from test_coordinated_runtime import _fake_model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "reconnect",
        "lost_process_state",
        "commit_unknown",
        "native_failure",
        "older_claims",
        "parent_retry",
    ],
)
async def test_public_acp_preplan_one_selected_write_after_verified_fake_native(
    monkeypatch, scenario
):
    with TemporaryDirectory(prefix="ac-acp-selected-write-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root = base / "wire"
        root.mkdir(mode=0o700)
        work = base / "work"
        work.mkdir(mode=0o700)
        resource = work / "module.py"
        resource.write_bytes(b"before\n")
        package = base / "fake-package"
        package.mkdir(mode=0o700)
        comms = Comms(root, private_initial_writes=True, private_claim_writes=True)
        for name, incarnation in (("sender", 51001.0), ("alpha", 51002.0), ("beta", 51003.0)):
            comms.register(
                Thread(
                    name, frozenset({"team"}), str(work), pid=os.getpid(), created_at=incarnation
                )
            )
        root_id = comms.initialize_private_initial_protocol()
        comms.initialize_private_claim_protocol()
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            install_private_response_schema(store)
            install_native_runtime_schema(store)
            install_prompt_binding_schema(store)
            for name in ("alpha", "beta"):
                store.register_participant(
                    stable_thread_lookup(comms.registry.require(name).created_at),
                    name,
                    name,
                    committed=True,
                )
        agent = CommsAgent(
            comms,
            runtime_enabled=True,
            private_nk_wire_root_id=root_id,
            private_nk_native_package=package,
        )
        agent._sessions["beta"] = "beta"
        agent._session_titles["beta"] = "beta"
        agent._session_worktrees["beta"] = str(work)
        agent._sessions["alpha"] = "alpha"
        agent._session_titles["alpha"] = "alpha"
        agent._session_worktrees["alpha"] = str(work)

        class AttachedClient:
            async def session_update(self, **_kwargs):
                return None

        agent._client = AttachedClient()  # explicit attached direct ACP test controller
        prior_seq = 0
        if scenario == "older_claims":
            for index in range(100):
                prior_seq = comms.send_message("sender", "#team", f"@beta earlier {index}").seq
        bus = MessageBus(root / "bus.jsonl", comms.registry, private_response_writes=True)
        if prior_seq:
            with MutationStore(str(root / "coordination.sqlite3")) as store:
                _accept_visible_initials(
                    bus, root_id, store, stable_thread_lookup(51003.0), 0, owner_name="beta"
                )
        message = comms.send_message("sender", "#team", "@beta inspect module.py")
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            _accept_visible_initials(
                bus, root_id, store, stable_thread_lookup(51003.0), prior_seq, owner_name="beta"
            )
        options = {
            "selectedExistingFileWrite": {
                "sourceSeq": message.seq,
                "sourceMessageId": message.message_id,
                "resource": str(resource),
                "contents": "after selected\n",
            }
        }
        # Public ACP prompt metadata is an operator intent with no text/model turn.
        with pytest.raises(IdentityConflict):
            await agent.prompt("alpha", [], field_meta={"agentComms": options})
        assert resource.read_bytes() == b"before\n"
        with pytest.raises(IdentityConflict, match="source identity changed"):
            await agent.prompt(
                "beta",
                [],
                field_meta={
                    "agentComms": {
                        "selectedExistingFileWrite": {
                            **options["selectedExistingFileWrite"],
                            "sourceMessageId": "wrong",
                        }
                    }
                },
            )
        with pytest.raises(IdentityConflict, match="matching private claim root"):
            SelectedWritePlans(comms, "foreign-root").submit(
                owner_name="beta",
                source_seq=message.seq,
                source_message_id=message.message_id,
                resource=str(resource),
                contents="not written",
            )
        if scenario == "parent_retry":
            original_sync = SelectedWritePlans._fsync_dir

            def fail_parent(path):
                if path == root:
                    raise OSError("ambiguous parent fsync")
                return original_sync(path)

            with monkeypatch.context() as fault:
                fault.setattr(SelectedWritePlans, "_fsync_dir", staticmethod(fail_parent))
                with pytest.raises(OSError, match="ambiguous parent fsync"):
                    await agent.prompt("beta", [], field_meta={"agentComms": options})
            assert (root / "selected-write-plans").is_dir()
            assert not list((root / "selected-write-plans").glob("*.json"))
        if scenario == "commit_unknown":
            original_sync = SelectedWritePlans._fsync_dir

            def fail_commit(path):
                if path.name == "selected-write-plans":
                    raise OSError("ambiguous directory fsync")
                return original_sync(path)

            with monkeypatch.context() as fault:
                fault.setattr(SelectedWritePlans, "_fsync_dir", staticmethod(fail_commit))
                with pytest.raises(OSError, match="ambiguous"):
                    await agent.prompt("beta", [], field_meta={"agentComms": options})
            with pytest.raises(IdentityConflict, match="already accepted or UNKNOWN"):
                await agent.prompt("beta", [], field_meta={"agentComms": options})
            assert resource.read_bytes() == b"before\n"
            assert len(list((root / "selected-write-plans").glob("*.json"))) == 1
            await agent.shutdown()
            return
        response = await agent.prompt("beta", [], field_meta={"agentComms": options})
        receipt = response.field_meta["agentComms"]["selectedWrite"]
        assert receipt["status"] == "accepted_not_applied"
        assert resource.read_bytes() == b"before\n"
        if scenario == "older_claims":
            assert message.seq > 100
            await agent.shutdown()
            return
        with pytest.raises(IdentityConflict, match="already accepted or UNKNOWN"):
            await agent.prompt("beta", [], field_meta={"agentComms": options})
        monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
        monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
        fake, calls = _fake_model(
            decision="FULL", fail_on=1 if scenario == "native_failure" else None
        )
        monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
        if scenario == "native_failure":
            with pytest.raises(NativePiUnavailable):
                await agent._drain_private_nk("beta", root_id)
            assert len(calls) == 1 and resource.read_bytes() == b"before\n"
            assert not list(Comms(root).claim_projection())
            await agent.shutdown()
            return
        if scenario in {"reconnect", "lost_process_state"}:
            if scenario == "reconnect":
                agent._client = AttachedClient()  # different ACP controller, same owner process
            else:
                agent._selected_write_controllers.clear()  # owner-process crash loses binding
            with pytest.raises(IdentityConflict, match="controller changed|no longer bound"):
                await agent._drain_private_nk("beta", root_id)
            assert not calls and resource.read_bytes() == b"before\n"
            rows = list((root / "selected-write-plans").glob("*.json"))
            assert len(rows) == 1 and json.loads(rows[0].read_text())["status"] == "accepted"
            await agent.shutdown()
            return
        assert await agent._drain_private_nk("beta", root_id) == 1
        assert len(calls) == 1 and resource.read_bytes() == b"after selected\n"
        claim = Comms(root).claim_projection()[str(resource)]
        assert (
            claim.admission is not None and claim.admission.operation_id == receipt["operationId"]
        )
        rows = list((root / "selected-write-plans").glob("*.json"))
        assert len(rows) == 1 and json.loads(rows[0].read_text())["status"] == "applied"
        assert await agent._drain_private_nk("beta", root_id) == 0
        assert len(calls) == 1
        await agent.shutdown()


@pytest.mark.asyncio
async def test_second_pid_public_acp_owner_ipc_preplan(monkeypatch):
    with TemporaryDirectory(prefix="ac-acp-public-selected-", dir="/var/tmp") as dirname:
        base = Path(dirname)
        base.chmod(0o700)
        root = base / "wire"
        root.mkdir(mode=0o700)
        work = base / "work"
        work.mkdir(mode=0o700)
        resource = work / "module.py"
        resource.write_bytes(b"before\n")
        package = base / "fake-package"
        package.mkdir(mode=0o700)
        comms = Comms(root, private_initial_writes=True, private_claim_writes=True)
        sender_pid = os.getpid()
        comms.register(Thread("sender", frozenset(), str(work), pid=sender_pid, created_at=61001.0))
        comms.register(
            Thread("alpha", frozenset({"team"}), str(work), pid=sender_pid, created_at=61002.0)
        )
        root_id = comms.initialize_private_initial_protocol()
        comms.initialize_private_claim_protocol()
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            install_private_response_schema(store)
            install_native_runtime_schema(store)
            install_prompt_binding_schema(store)
            store.register_participant(
                stable_thread_lookup(61002.0), "alpha", "alpha", committed=True
            )
        env = dict(os.environ)
        source_root = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = f"{source_root / 'src'}:{source_root / 'tests'}"
        env.update(
            AGENT_COMMS_ROOT=str(root),
            AGENT_COMMS_THREAD="beta",
            AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
            AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.pop("PI_PROMPT", None)
        child = Path(__file__).with_name("selected_write_owner_child.py")
        process = subprocess.Popen(
            [sys.executable, "-u", str(child), str(base)],
            env=env,
            cwd=work,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        attached = None
        try:
            assert process.pid != sender_pid
            comms.register(
                Thread("beta", frozenset({"team"}), str(work), pid=process.pid, created_at=61003.0)
            )
            with MutationStore(str(root / "coordination.sqlite3")) as store:
                store.register_participant(
                    stable_thread_lookup(61003.0), "beta", "beta", committed=True
                )
            (base / "registered").touch()

            def event(expected: str) -> dict:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError(
                            f"worker exited {process.returncode}: {process.stderr.read()[-1000:]}"
                        )
                    readable, _, _ = select.select(
                        [process.stdout], [], [], max(0.01, deadline - time.monotonic())
                    )
                    if readable:
                        line = process.stdout.readline()
                        if not line:
                            process.wait(timeout=3)
                            raise AssertionError(
                                f"worker EOF {process.returncode}: {process.stderr.read()[-1600:]}"
                            )
                        row = json.loads(line)
                        if row["event"] == expected:
                            return row
                raise AssertionError(f"worker did not report {expected}")

            assert event("ready")["pid"] == process.pid
            attached = CommsAgent(
                comms, private_nk_wire_root_id=root_id, private_nk_native_package=package
            )
            await attached.load_session(str(work), "beta")
            message = comms.send_message("sender", "#team", "@beta inspect module.py")
            bus = MessageBus(root / "bus.jsonl", comms.registry, private_response_writes=True)
            with MutationStore(str(root / "coordination.sqlite3")) as store:
                _accept_visible_initials(
                    bus, root_id, store, stable_thread_lookup(61003.0), 0, owner_name="beta"
                )
            options = {
                "selectedExistingFileWrite": {
                    "sourceSeq": message.seq,
                    "sourceMessageId": message.message_id,
                    "resource": str(resource),
                    "contents": "after second pid\n",
                }
            }
            response = await attached.prompt("beta", [], field_meta={"agentComms": options})
            assert (
                response.field_meta["agentComms"]["selectedWrite"]["status"]
                == "accepted_not_applied"
            )
            assert resource.read_bytes() == b"before\n"
            (base / "dispatch").touch()
            result = event("terminal")
            assert result["pid"] == process.pid and result["drained"] == 1
            assert result["fake_inputs"] == 1 and resource.read_bytes() == b"after second pid\n"
            owner = Comms(root).claim_projection()[str(resource)]
            assert owner.owner == "beta" and owner.admission is not None
            with MutationStore(str(root / "coordination.sqlite3")) as store:
                assert store.claim(owner.admission.wake_claim_id).disposition.value == "completed"
                assert (
                    store._connection.execute(
                        "SELECT COUNT(*) FROM native_runtime_inputs"
                    ).fetchone()[0]
                    == 1
                )
                assert sealed_cohort_claims(store, stable_thread_lookup(61002.0)) == ()
        finally:
            if attached is not None:
                await attached.shutdown()
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)  # fixture process only
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()  # fixture process only
                process.communicate(timeout=5)
