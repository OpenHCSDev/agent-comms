"""Default-OFF public ACP operator preplan; selected file bytes are not model tools."""

from __future__ import annotations

from conftest import responder

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    SelectedWriteAcceptedUpdate,
    SelectedWriteRequest,
    decode_updates,
    encode_request,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.launch_root import LaunchRoot
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import sealed_cohort_assignments
from agent_comms.envelope_claim_transitions import UnclaimedResource
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.native_prompt_binding import install_prompt_binding_schema
from agent_comms.native_runtime_input import NativeRuntimeInput
from native_backend_fixture import native_backend_fixture

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or Path("/var").is_symlink() or Path("/var/tmp").is_symlink(),
    reason="selected private root requires a physical POSIX /var/tmp",
)


@pytest.mark.asyncio
async def test_second_pid_public_acp_owner_ipc_preplan(tmp_path, native_runtime_package):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        base = tmp_path
        root, work = native.root, native.project
        resource = work / "module.py"
        resource.write_bytes(b"before\n")
        comms = Comms(root)
        sender_pid = os.getpid()
        comms.registry.declare(
            comms.registry.new_thread('sender', frozenset(), str(work), process_identity=ProcessIdentity.capture(sender_pid), created_at=61001.0)
        )
        alpha = comms.registry.declare(
            comms.registry.new_thread('alpha', frozenset({'team'}), str(work), process_identity=ProcessIdentity.capture(sender_pid), created_at=61002.0, model='response-local/fixture')
        )
        root_id = comms.owners.require_private_launch().wire_root_id
        with Coordination(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            install_private_response_schema(store)
            install_native_runtime_schema(store)
            install_prompt_binding_schema(store)
            store.participants.register(
                alpha.incarnation.lookup, "alpha", "alpha", committed=True
            )
        env = dict(os.environ)
        source_root = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = f"{source_root / 'src'}:{source_root / 'tests'}"
        env.update(
            AGENT_COMMS_THREAD="beta",
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.pop("PI_PROMPT", None)
        child = Path(__file__).with_name("selected_write_owner_child.py")
        stderr_path = base / "worker-stderr.log"
        stderr_file = stderr_path.open("w")
        process = subprocess.Popen(
            [sys.executable, "-u", str(child), str(base), *LaunchRoot.from_path(root).arguments()],
            env=env,
            cwd=work,
            text=True,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            start_new_session=True,
        )
        attached = None
        try:
            assert process.pid != sender_pid
            beta = comms.registry.declare(
                comms.registry.new_thread('beta', frozenset({'team'}), str(work), process_identity=ProcessIdentity.capture(process.pid), created_at=61003.0, model='response-local/fixture')
            )
            with Coordination(str(root / "coordination.sqlite3")) as store:
                store.participants.register(
                    beta.incarnation.lookup, "beta", "beta", committed=True
                )
            (base / "registered").touch()

            async def event(expected: str) -> dict:
                try:
                    async with asyncio.timeout(20):
                        while True:
                            line = await asyncio.to_thread(process.stdout.readline)
                            if not line:
                                await asyncio.to_thread(process.wait, timeout=3)
                                raise AssertionError(
                                    f"worker EOF {process.returncode}: {stderr_path.read_text()[-1600:]}"
                                )
                            row = json.loads(line)
                            if row["event"] == expected:
                                return row
                except TimeoutError as error:
                    raise AssertionError(
                        f"worker did not report {expected}: {stderr_path.read_text()[-1600:]}"
                    ) from error

            assert (await event("ready"))["pid"] == process.pid
            attached = CommsAgent(
                comms,
            )
            await attached.load_session(str(work), "beta")
            message = comms.messaging.send("sender", "#team", "@beta inspect module.py").message
            bus = responder(root, comms.registry)
            with Coordination(str(root / "coordination.sqlite3")) as store:
                await _accept_visible_deliveries(
                    bus, root_id, store.session.path, beta.incarnation.lookup, 0, owner_name="beta"
                )
            options = encode_request(
                SelectedWriteRequest(
                    message.seq, message.message_id, str(resource), "after second pid\n"
                )
            )
            response = await attached.prompt("beta", [], field_meta=options)
            (acknowledgement,) = decode_updates(response.field_meta)
            assert isinstance(acknowledgement, SelectedWriteAcceptedUpdate)
            assert resource.read_bytes() == b"before\n"
            (base / "dispatch").touch()
            result = await event("terminal")
            assert result["pid"] == process.pid and result["drained"] == 1
            assert native.provider.posts == 1 and resource.read_bytes() == b"after second pid\n"
            with Comms(root).bus.log.checked_read() as source:
                assert source.claim_operation_used(acknowledgement.operation_id)
                assert source.claim(str(resource)) == UnclaimedResource()
            rows = list((root / "selected-write-plans").glob("*.json"))
            assert len(rows) == 1 and json.loads(rows[0].read_text())["status"] == "applied"
            with Coordination(str(root / "coordination.sqlite3")) as store:
                selected = sealed_cohort_assignments(
                    store, beta.incarnation.lookup
                )
                assert len(selected) == 1 and selected[0].lifecycle.declared_name == "completed"
                assert (
                    store.session._connection.execute(
                        f"SELECT COUNT(*) FROM {NativeRuntimeInput.declared_name}"
                    ).fetchone()[0]
                    == 1
                )
                assert sealed_cohort_assignments(store, alpha.incarnation.lookup) == ()
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
            stderr_file.close()
