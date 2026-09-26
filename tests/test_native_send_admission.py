"""Actual adapter write/drain scope; no provider or native acceptance inferred."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms import native_pi
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordinated_runtime import run_one_sealed_claim
from agent_comms.coordination_store import MutationStore, StaleFence
from test_coordinated_runtime import _fake_model, _root
from test_coordinated_runtime import tmp_path as private_root_fixture

# Reuse the safe /var/tmp fixture rather than pytest's possibly public /tmp.
tmp_path = private_root_fixture


@pytest.mark.parametrize(
    ("direct", "drift"), [(False, "generation"), (False, "registry"), (True, "registry")]
)
async def test_pre_send_drift_refuses_all_prompt_bytes(tmp_path, monkeypatch, direct, drift):
    root, root_id, comms, _, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")

    async def race(*args, **kwargs):
        if drift == "generation":
            with MutationStore(str(root / "coordination.sqlite3")) as store:
                store.advance_owner_generation(
                    stable_thread_lookup(owner.created_at), owner.name, expected_generation=1
                )
        else:
            comms.registry.unregister(owner.name)
        return await fake(*args, **kwargs)

    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", race)
    with pytest.raises(StaleFence):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name=owner.name, native_package=tmp_path
        )
    assert calls == []


# A separate process attempts the exact canonical exclusions, without waiting.
# It reports whether locks are held; no scheduler-timing or sleep assertion.
_PROBE = r"""
import fcntl, json, sqlite3, sys
from agent_comms.declarations import _store_lock
from pathlib import Path
root = Path(sys.argv[1])
held = []
for name in ('wire', 'bus.jsonl', 'registry.json'):
    # _store_lock uses the .lock suffix next to the actual store.
    with open(root / ('.' + name + '.lock'), 'a+b') as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(name)
db = sqlite3.connect(root / 'coordination.sqlite3', timeout=0)
try:
    db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as e:
    if 'locked' not in str(e):
        raise
    held.append('sql')
finally:
    db.close()
print(json.dumps(held))
"""


def _held(root: Path) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("revoke", [False, True])
async def test_actual_native_write_and_drain_hold_owner_exclusions(
    tmp_path, monkeypatch, direct, revoke
):
    root, root_id, comms, _, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    sent = []
    observed = []
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))

    class Stdin:
        def write(self, raw):
            command = json.loads(raw)
            sent.append(command)
            if command["type"] == "prompt":
                observed.append(("write", _held(root)))
            elif revoke:
                comms.registry.unregister(owner.name)

        async def drain(self):
            if sent[-1]["type"] == "prompt":
                await asyncio.sleep(0)  # yield once within the actual adapter drain
                observed.append(("drain", _held(root)))

        def close(self):
            pass

    class Process:
        def __init__(self):
            self.stdin = Stdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = 0
            session = root / "native-sessions" / stable_thread_lookup(owner.created_at) / "s.jsonl"
            self.stdout.feed_data(
                (
                    json.dumps(
                        {
                            "type": "response",
                            "id": "native-capability",
                            "command": "get_state",
                            "success": True,
                            "data": {
                                "nativeInputProofCapability": native_pi.CAPABILITY,
                                "sessionId": "session",
                                "sessionFile": str(session),
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return 0

    async def launch(*args, **kwargs):
        return Process()

    monkeypatch.setattr(native_pi.asyncio, "create_subprocess_exec", launch)
    # Fake child emits no receipt; rejection after send is expected, never success.
    expected_error = StaleFence if revoke else native_pi.NativePiUnavailable
    with pytest.raises(expected_error):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name=owner.name, native_package=tmp_path
        )
    if revoke:
        assert [item["type"] for item in sent] == ["get_state"]
        assert observed == []
    else:
        assert [item["type"] for item in sent] == ["get_state", "prompt"]
        assert observed == [
            ("write", ["wire", "bus.jsonl", "registry.json", "sql"]),
            ("drain", ["wire", "bus.jsonl", "registry.json", "sql"]),
        ]
    assert _held(root) == []
