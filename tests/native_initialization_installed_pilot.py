"""Actual fresh Pi IPC, configured R1 model, zero prompt/provider requests.

Run with the installed interpreter and --root under the owned persistent WT.
This observes initialization only; it grants no input retry authority.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from agent_comms.diagnostics import record_terminal_failure
from agent_comms.native_custody import PiSessionChild
from agent_comms.native_pi import NativePiRpcLaunch, NativePiUnavailable
from agent_comms.native_startup import NativeStartupAdmission
from agent_comms.selected_session import SelectedSession


async def run(root: Path, package: Path) -> dict:
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    project = root / "project"
    project.mkdir(mode=0o700)
    os.environ["AGENT_COMMS_ROOT"] = str(root)
    launch = NativePiRpcLaunch.tracked(
        package,
        worktree=project,
        session=SelectedSession(root / "native"),
        provider="openrouter",
        model="z-ai/glm-5.3-flash",
        thinking_level="off",
    )
    admission = NativeStartupAdmission(root)
    started = time.monotonic()
    await admission.acquire()
    child = await PiSessionChild.start((launch, launch.configuration.auth_revision()), None)
    spawned = time.monotonic()
    request = child.attestation.request
    receipt = {
        "provider": "openrouter",
        "model": "z-ai/glm-5.3-flash",
        "thinking": "off",
        "prompt_commands": 0,
        "control_commands": [request.declared_name],
        "pid": child.proc.pid,
        "spawn_ms": round((spawned - started) * 1000),
    }
    try:
        assert child.proc.stdin is not None
        async with asyncio.timeout(admission.policy.readiness_seconds):
            event = await request.exchange(child.reader, child.proc.stdin, strict=True)
        child.attestation = child.attestation.accept(event)
        state = child.attestation.state
        assert state is not None and state.message_count == state.pending_message_count == 0
        receipt.update(
            ok=True,
            attested_ms=round((time.monotonic() - spawned) * 1000),
            native_input_capability=state.native_input_proof_capability,
            message_count=state.message_count,
            pending_message_count=state.pending_message_count,
        )
    except Exception as error:
        await child.close()
        stderr = await child.stderr_task
        failure = NativePiUnavailable(
            f"Fresh get_state failed before any prompt: {type(error).__name__}; "
            f"native stderr={stderr or '(empty)'}"
        )
        failure.__cause__ = error
        diagnostic = record_terminal_failure(
            root,
            turn_id=uuid4().hex,
            thread="private-r1-initialization",
            event={"reason_code": "native_preflight_timeout"},
            sequences=(),
            source_error=failure,
        )
        receipt.update(ok=False, error_type=type(error).__name__, diagnostic=diagnostic.name)
    finally:
        admission.release()
        child.reader.pending.cancel_all()
        await child.close()
        receipt["stderr_bytes"] = len((await child.stderr_task).encode())
        receipt["child_retired"] = not child.proc.identity.alive()
        receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        path = root / "receipt.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(receipt, output, indent=2)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()
    receipt = asyncio.run(run(args.root.resolve(), args.package.resolve()))
    print(json.dumps(receipt))
    return 0 if receipt["ok"] and receipt["child_retired"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
