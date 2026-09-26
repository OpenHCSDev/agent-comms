"""Read-only verified pinned Pi get_state probe in disposable PID namespace.

No prompt, provider request, credentials, live root, or returned receipt. Usage:
  env -i HOME="$HOME" USER="$USER" PATH="$PATH" PYTHONPATH=src \
    python experiments/native_pi_retirement_compiled_probe.py
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from agent_comms.native_pi import _trusted_package, prepare_native_pi_rpc_launch
from agent_comms.native_pi_retirement import RetirementIdentity, spawn_retired_child
from agent_comms.native_prompt_binding import native_request_digest


async def probe() -> dict[str, object]:
    packages = sorted(
        Path("/var/tmp").glob(
            "agent-comms-pi-native-*/node_modules/@earendil-works/pi-coding-agent"
        )
    )
    package = None
    for candidate in packages:
        try:
            _trusted_package(candidate)
        except Exception:
            continue
        package = candidate
        break
    if package is None:
        return {"status": "unavailable", "reason": "no reviewed disposable pinned Pi fork"}
    with tempfile.TemporaryDirectory(prefix="ac-retire-compiled-", dir="/var/tmp") as raw:
        root = Path(raw)
        session_dir = root / "sessions"
        session_dir.mkdir(mode=0o700)
        session_file = session_dir / "one.jsonl"
        session_file.write_text('{"type":"session","id":"sid"}\n')
        session_file.chmod(0o600)
        launch = prepare_native_pi_rpc_launch(
            package, worktree=root, session_dir=session_dir, session_file=session_file
        )
        if launch.env.get("PI_OFFLINE") != "1" or any(
            any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "AWS_"))
            for key in launch.env
        ):
            raise RuntimeError("credential-bearing environment is forbidden for this probe")
        input_id = "a" * 32
        prompt = "never dispatched"
        identity = RetirementIdentity(
            wire_root_id="1" * 32,
            input_id=input_id,
            stage="full",
            claim_id="probe-claim",
            execution_id="probe-execution",
            attempt_ordinal=1,
            owner_thread="probe",
            recipient_lookup="2" * 32,
            owner_created_at=17_000.0,
            owner_pid=os.getpid(),
            owner_generation=1,
            owner_admission_epoch=1,
            owner_turn_id="probe-turn",
            source_seq=1,
            source_message_id="probe-message",
            native_request_digest=native_request_digest(prompt),
            session_file=session_file,
        )
        child = await spawn_retired_child(launch, identity, input_id, prompt)
        observed = b""
        try:
            assert child.process.stdin is not None and child.process.stdout is not None
            child.process.stdin.write(b'{"type":"get_state","id":"probe-state"}\n')
            await asyncio.wait_for(child.process.stdin.drain(), timeout=5)
            with suppress(TimeoutError):
                observed = await asyncio.wait_for(child.process.stdout.readline(), timeout=5)
        finally:
            receipt = await child.retire(None)
            assert receipt is None  # no prompt was sent, so no input can settle
        assert child.process.stderr is not None
        stderr = (await asyncio.wait_for(child.process.stderr.read(8192), timeout=2)).decode(
            errors="replace"
        )
        return {
            "status": "rpc-get-state" if observed else "no-rpc-get-state",
            "verified_disposable_package": str(package),
            "rpc": observed.decode(errors="replace")[:1000],
            "stderr": stderr[:3000],
            "returned_receipt": False,
        }


if __name__ == "__main__":
    if any(
        any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "AWS_"))
        for key in os.environ
    ):
        raise SystemExit("Probe must run with a credential-free environment")
    print(json.dumps(asyncio.run(probe()), sort_keys=True))
