"""Real connector HTTP refusal on a copied, maintenance-held bus; no test doubles.

PYTHONPATH=src python -X faulthandler tests/verify_connector_maintenance.py --scratch DIR
Only private scratch and the loopback server are changed; no installed service is restarted.
"""
from __future__ import annotations


from agent_comms.bus import Bus

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.connector_store import ConnectorStore
from agent_comms.maintenance_barrier import (
    InstallingPhase, MaintenanceBarrier, MaintenanceOperator, PausedPhase,
)
from agent_comms.store_files import _store_lock
from connector_readiness import ConnectorReadiness
from verify_connector_live import curl_call


def prove(scratch: Path) -> None:
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="maintenance-proof-", dir=scratch) as directory:
        base = Path(directory)
        original = wire(base / "original")
        for name in ("claude-ai", "worker"):
            original.registry.declare(original.registry.new_thread(name, frozenset(), str(base)))
        Bus.install(Bus.path_in(original.root))
        original.messaging.send("worker", "claude-ai", "Ready for your message")
        for worker in threading.enumerate():
            if worker.name == "agent-comms-candidate-maintenance":
                worker.join(timeout=10)
                assert not worker.is_alive()
        before = original.bus.log.path.read_bytes()
        copied = base / "copied"
        shutil.copytree(original.root, copied)
        home = base / "home"
        auth = home / ".local/state/agent-comms/connector"
        auth.mkdir(mode=0o700, parents=True)
        store = ConnectorStore.at(auth)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        project = Path(__file__).resolve().parents[1]
        readiness = ConnectorReadiness()
        environment = readiness.environment({
            **os.environ, "HOME": str(home), "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(project / "src"),
        })
        entrypoint = shutil.which("agent-comms-connector")
        assert entrypoint, "the installed connector entrypoint is required"
        error_path = base / "server.log"
        with error_path.open("w") as errors:
            service = subprocess.Popen(
                [entrypoint, "serve", "--root", str(copied), "--port", str(port)],
                env=environment, stdout=subprocess.PIPE, stderr=errors, text=True,
            )
            try:
                readiness.wait(service, port)
                barrier = MaintenanceBarrier(copied / "registry.json")
                held = barrier.begin(MaintenanceOperator.current())
                held = barrier.advance(held, PausedPhase)
                held = barrier.advance(held, InstallingPhase)
                with store.granted("private maintenance proof") as token:
                    with _store_lock(barrier.wire_path), _store_lock(barrier.registry_path):
                        started = time.monotonic()
                        output = curl_call(port, token, "Please read this message")
                        wall = time.monotonic() - started
                        response, timing = output.strip().rsplit("\n", 1)
                        status, curl_seconds = timing.split()
                        reply = json.loads(response)
                        result = reply["result"]
                        refusal = json.loads(result["content"][0]["text"])
                        assert status == "200" and result["isError"] is True, output
                        assert refusal == {
                            "type": "held",
                            "error": "Maintenance holds ingress: the bus is under maintenance",
                        }, output
                        assert wall < 0.1 and float(curl_seconds) < 0.1, output
                        assert barrier.current_unlocked() == held
                        assert (copied / "bus.jsonl").read_bytes() == before
                print(json.dumps({
                    "entrypoint": entrypoint, "core_source": str(project / "src"),
                    "phase": held.phase.declared_name,
                    "wire_and_registry_locks": "held exclusively throughout HTTP call",
                    "tool": "comms_send", "status": int(status),
                    "curl_seconds": float(curl_seconds), "wall_seconds": wall,
                    "refusal": refusal, "copied_bus_unchanged": True,
                    "original_bus_unchanged": original.bus.log.path.read_bytes() == before,
                }, indent=2))
                assert original.bus.log.path.read_bytes() == before
                assert not original.owners.maintenance.read().turn_pending
            finally:
                service.terminate()
                service.communicate(timeout=15)
                readiness.close()
        assert "Error handling request" not in error_path.read_text(), error_path.read_text()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", required=True, type=Path)
    prove(parser.parse_args().scratch)


if __name__ == "__main__":
    main()
