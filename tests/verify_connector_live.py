"""Real scratch-bus connector/curl and maintenance proof; no test doubles.

PYTHONPATH=src python -X faulthandler tests/verify_connector_live.py
All scratch is owned by pi-connector and removed after the subprocess exits.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from agent_comms import candidate_maintenance as maintenance
from agent_comms.comms import Comms
from agent_comms.connector_store import ConnectorStore
from agent_comms.wake_candidate_index import WakeCandidateIndex, OutdatedCheckpoint


def wait_started(service, log):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ready, _, _ = select.select([service.stdout], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        line = service.stdout.readline()
        log.append(line)
        if "Running on" in line:
            return
        if not line:
            break
    raise RuntimeError("Connector failed to start: " + "".join(log))


def curl_call(port, token, body, *, arguments=None):
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "comms_send", "arguments": arguments if arguments is not None else {
            "from": "claude-ai", "to": "worker", "body": body}}}
    return subprocess.run([
        "curl", "--silent", "--show-error", "--max-time", "15", "-w", "\n%{http_code} %{time_total}\n",
        "-H", "Content-Type: application/json", "-H", f"Authorization: Bearer {token}",
        "--data", json.dumps(call), f"http://127.0.0.1:{port}/mcp",
    ], capture_output=True, text=True, check=True).stdout


def maintenance_proof(comms):
    log = comms.bus.log
    # A real committed original is indexed by the normal background worker.
    message = comms.messaging.send("worker", "claude-ai", "scratch original").message
    maintenance.drain_candidate_catchup(comms.root)
    index = WakeCandidateIndex(log)
    root_id = log.private_marker_unlocked().root_id
    before = index._verified_checkpoint(root_id)
    replacement = log.path.with_name("replacement.jsonl")
    replacement.write_bytes(log.path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(log.path)
    # The canonical source owner, not the projection, approves the same prefix
    # at its replacement inode, exactly as the source barrier requires.
    with log.checked_read() as checked:
        checked.require_current()
    assert isinstance(index._captured_checkpoint(root_id), OutdatedCheckpoint)
    print(f"source replacement: old inode={before.inode}, new inode={log.path.stat().st_ino}; outdated checkpoint")
    failures = []
    observed = []
    entered = threading.Event()
    old_hook = threading.excepthook

    def failed(args):
        failures.append(f"{args.exc_type.__name__}: {args.exc_value}")

    def profile(frame, event, arg):
        if frame.f_code is maintenance.CandidateMaintenance.run.__code__ and event == "call":
            observed.append(threading.current_thread())
            entered.set()

    threading.excepthook = failed
    threading.setprofile(profile)
    try:
        maintenance.schedule_candidate_catchup(log, log.path.stat().st_size)
        assert entered.wait(5)
        worker, = observed
        worker.join(timeout=15)
        assert not worker.is_alive(), "maintenance did not finish its drain"
    finally:
        threading.setprofile(None)
        threading.excepthook = old_hook
    print(f"maintenance threads started={len(observed)}, failures={failures}")
    assert not failures
    after = index._verified_checkpoint(root_id)
    assert after.inode == log.path.stat().st_ino and after.last_seq == message.seq
    assert after.byte_offset == log.path.stat().st_size
    assert log.path.parent not in maintenance._pending
    print(f"same maintenance thread completed normally: last_seq={after.last_seq}, caught_up=True, no restart")


def main():
    scratch = Path.home() / ".cache/agent-scratch/pi-connector"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="live-", dir=scratch) as directory:
        base = Path(directory)
        root = base / "wire"
        root.mkdir(mode=0o700)
        comms = Comms(root)
        for name in ("claude-ai", "worker"):
            comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(base)))
        comms.messaging.initialize_private_initial_protocol()
        home = base / "home"
        store_root = home / ".local/state/agent-comms/connector"
        store_root.mkdir(mode=0o700, parents=True)
        store = ConnectorStore.at(store_root)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {**os.environ, "HOME": str(home), "PYTHONUNBUFFERED": "1"}
        # Keep installed entrypoint and interpreter, changing only the source
        # path selected by the caller. Never publish or restart the live service.
        entrypoint = shutil.which("agent-comms-connector")
        error_path = base / "server.log"
        errors = error_path.open("w")
        service = subprocess.Popen([
            entrypoint, "serve", "--root", str(root), "--port", str(port),
        ], env=environment, stdout=subprocess.PIPE, stderr=errors, text=True)
        server_log = []
        try:
            wait_started(service, server_log)
            print(f"source={Path(sys.modules['agent_comms.comms'].__file__).parent}")
            print("entrypoint=agent-comms-connector serve --root <scratch> --port <loopback-port>")
            with store.granted("scratch curl proof") as token:
                before = comms.bus.log.latest_sequence()
                output = curl_call(port, token, "Fix the candidate now")
                print("published owner wording:\n" + output.strip())
                response, status = output.strip().rsplit("\n", 1)
                assert status.split()[0] == "200"
                assert json.loads(response)["result"]["isError"] is False
                messages = comms.bus.inbox("worker")
                assert len(messages) == 1
                assert messages[0].sender == "claude-ai"
                assert messages[0].body == "Fix the candidate now"
                assert messages[0].seq > before
                read_command = [
                    shutil.which("agent-comms"), "--root", str(root), "tool",
                    "--tool", "comms_send_refusals", "--arguments",
                    json.dumps({"sender": "claude-ai"}),
                ]
                readback = subprocess.run(
                    read_command, env=environment, capture_output=True, text=True)
                assert readback.returncode == 0, readback.stdout + readback.stderr
                assert json.loads(readback.stdout)["refusals"] == []
                print("published owner wording: refusal rows=0")

                # Unknown destinations are still genuine refused sends. Exercise
                # the real connector path, not a fabricated refusal-table entry.
                before = comms.bus.log.latest_sequence()
                output = curl_call(port, token, "", arguments={
                    "from": "claude-ai", "to": "#missing-channel", "body": "Fix this now",
                })
                response, status = output.strip().rsplit("\n", 1)
                result = json.loads(response)["result"]
                assert status.split()[0] == "200" and result["isError"] is True
                assert "Unknown channel" in response
                assert comms.bus.log.latest_sequence() == before, "a refused send committed bytes"
                readback = subprocess.run(
                    read_command, env=environment, capture_output=True, text=True)
                assert readback.returncode == 0, readback.stdout + readback.stderr
                rows = json.loads(readback.stdout)["refusals"]
                assert len(rows) == 1, "each refused send must record exactly once"
                assert rows[0]["sender"] == "claude-ai"
                assert rows[0]["target"] == "#missing-channel"
                assert rows[0]["reason"] == json.loads(result["content"][0]["text"])["error"]
                assert rows[0]["refused_at_ms"] > 0
                print("unknown-target refusal readback: " + json.dumps(rows[0], sort_keys=True))
                output = curl_call(port, token, "", arguments={"unexpected": True})
                response, status = output.strip().rsplit("\n", 1)
                assert status.split()[0] == "200" and json.loads(response)["result"]["isError"]
                assert "Invalid comms_send arguments" in response
                print("invalid tool arguments: " + status)
        finally:
            service.terminate()
            tail, _ = service.communicate(timeout=15)
            server_log.append(tail)
            errors.close()
            print("connector stopped; HTTP handler tracebacks=" + str(error_path.read_text().count("Error handling request")))
        maintenance_proof(comms)


if __name__ == "__main__":
    main()
