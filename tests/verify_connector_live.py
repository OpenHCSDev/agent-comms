"""Real scratch-bus connector/curl and maintenance proof; no test doubles.

PYTHONPATH=src python -X faulthandler tests/verify_connector_live.py <owned-scratch>
The caller selects scratch; each subprocess run retires its temporary directory.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from connector_readiness import ConnectorReadiness
from agent_comms import candidate_maintenance as maintenance
from agent_comms.comms import Comms
from agent_comms.connector_store import ConnectorStore
from agent_comms.wake_candidate_index import WakeCandidateIndex


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
    bus = comms.bus.bus
    message = comms.messaging.send("worker", "claude-ai", "scratch original").message
    maintenance.schedule_candidate_catchup(bus, message.seq)
    maintenance.drain_candidate_catchup(comms.root)
    index = WakeCandidateIndex(bus)
    assert index.through_seq >= message.seq
    assert bus.path.parent not in maintenance._pending
    print(f"maintenance completed: scanned seq={index.through_seq}, committed seq={message.seq}")


def main(scratch: Path):
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
        readiness = ConnectorReadiness()
        environment = readiness.environment({**os.environ, "HOME": str(home), "PYTHONUNBUFFERED": "1"})
        # Keep installed entrypoint and interpreter, changing only the source
        # path selected by the caller. Never publish or restart the live service.
        entrypoint = shutil.which("agent-comms-connector")
        error_path = base / "server.log"
        errors = error_path.open("w")
        service = subprocess.Popen([
            entrypoint, "serve", "--root", str(root), "--port", str(port),
        ], env=environment, stdout=subprocess.PIPE, stderr=errors, text=True)
        try:
            readiness.wait(service, port)
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
            service.communicate(timeout=15)
            errors.close()
            readiness.close()
            print("connector stopped; HTTP handler tracebacks=" + str(error_path.read_text().count("Error handling request")))
        maintenance_proof(comms)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
