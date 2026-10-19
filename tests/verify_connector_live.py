"""Real scratch-bus connector/curl and maintenance proof; no test doubles.

PYTHONPATH=src python -X faulthandler tests/verify_connector_live.py
Use --baseline with PYTHONPATH pointing at main to record its failures.
All scratch is owned by pi-connector and removed after the subprocess exits.
"""
from __future__ import annotations

import argparse
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
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviews
from agent_comms.wake_candidate_index import WakeCandidateIndex, ProjectionRebuildRequiredError


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


def reviews_at(reviews, age):
    reviews.write("claude-ai", DrawnRuleReviewEntry(("Declare each fact once",), 0))
    reviews.write("claude-ai", AnsweredRuleReviewEntry(
        1, "This scratch run checks connector.py:66 without sending to any real user thread."))
    with reviews.writing() as db:
        db.execute("UPDATE drawn_rule SET drawn_at=? WHERE reviewer=?", (time.time() - age, "claude-ai"))


def curl_call(port, token, body, *, arguments=None):
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "comms_send", "arguments": arguments if arguments is not None else {
            "from": "claude-ai", "to": "worker", "body": body}}}
    return subprocess.run([
        "curl", "--silent", "--show-error", "--max-time", "15", "-w", "\n%{http_code} %{time_total}\n",
        "-H", "Content-Type: application/json", "-H", f"Authorization: Bearer {token}",
        "--data", json.dumps(call), f"http://127.0.0.1:{port}/mcp",
    ], capture_output=True, text=True, check=True).stdout


def maintenance_proof(comms, baseline):
    log = comms.bus.log
    # A real committed original is indexed by the normal background worker.
    message = comms.messaging.send("worker", "claude-ai", "scratch original").message
    for worker in threading.enumerate():
        if worker.name == "agent-comms-candidate-maintenance":
            worker.join(timeout=15)
            assert not worker.is_alive()
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
    try:
        index._verified_checkpoint(root_id)
    except ProjectionRebuildRequiredError as error:
        print(f"source replacement: old inode={before.inode}, new inode={log.path.stat().st_ino}; {error}")
    else:
        raise AssertionError("scratch source did not invalidate the projection")
    failures = []
    observed = []
    entered = threading.Event()
    old_hook = threading.excepthook

    def failed(args):
        failures.append(f"{args.exc_type.__name__}: {args.exc_value}")

    def profile(frame, event, arg):
        if frame.f_code is maintenance._drain_candidate.__code__ and event == "call":
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
    if baseline:
        assert failures and "candidate bus source changed" in failures[0]
        return
    assert not failures
    after = index._verified_checkpoint(root_id)
    assert after.inode == log.path.stat().st_ino and after.last_seq == message.seq
    assert after.byte_offset == log.path.stat().st_size
    assert log.path.parent not in maintenance._pending
    print(f"same maintenance thread completed normally: last_seq={after.last_seq}, caught_up=True, no restart")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
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
        reviews = RuleReviews(root)
        reviews_at(reviews, 0)
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
                for label, age, body in (
                    ("refused wording", 0, "Fix the candidate now"),
                    ("expired review", 21 * 60, "Fix the send now"),
                ):
                    reviews_at(reviews, age)
                    output = curl_call(port, token, body)
                    print(label + ":\n" + output.strip())
                    response, status = output.strip().rsplit("\n", 1)
                    code, seconds = status.split()
                    assert code == ("500" if args.baseline else "200")
                    if not args.baseline:
                        result = json.loads(response)["result"]
                        assert result["isError"] is True
                        if age:
                            assert "last rule review was 21 min ago (max 20)" in response
                            assert "agent-comms-commander rules review" in response
                            assert float(seconds) < 1
                        else:
                            assert "Jargon" in response and "candidate" in response
                assert comms.bus.log.latest_sequence() == before, "a refused send committed bytes"
                if not args.baseline:
                    output = curl_call(port, token, "", arguments={"unexpected": True})
                    response, status = output.strip().rsplit("\n", 1)
                    assert status.split()[0] == "200" and json.loads(response)["result"]["isError"]
                    assert "Invalid comms_send arguments" in response
                    print("invalid tool arguments: " + status)
                    # An actual malformed stored schema is a real error, not a
                    # user refusal. It must not be caught by the MCP tool boundary.
                    with reviews.writing() as db:
                        db.execute("DROP TABLE rule_answer")
                    output = curl_call(port, token, "Fix the send now")
                    assert output.strip().rsplit("\n", 1)[1].split()[0] == "500"
                    print("malformed stored schema remains HTTP 500")
        finally:
            service.terminate()
            tail, _ = service.communicate(timeout=15)
            server_log.append(tail)
            errors.close()
            print("connector stopped; HTTP handler tracebacks=" + str(error_path.read_text().count("Error handling request")))
        maintenance_proof(comms, args.baseline)


if __name__ == "__main__":
    main()
