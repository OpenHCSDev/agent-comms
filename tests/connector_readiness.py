"""Systemd's side of Type=notify for a connector a test starts: it serves once READY=1 arrives."""
from __future__ import annotations

import os
import secrets
import selectors
import socket
import subprocess
import time
from collections.abc import Mapping

from agent_comms.host_services import ServiceNotServingError, SystemdSite


class ConnectorReadiness:
    """One notification socket; the connector reports READY=1 on it after binding its port."""

    def __init__(self):
        # An abstract socket, as systemd may use: no file, and no sun_path limit on long scratch paths.
        self.name = f"agent-comms-readiness-{secrets.token_hex(8)}"
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind("\0" + self.name)

    def environment(self, base: Mapping[str, str]) -> dict[str, str]:
        return {**base, SystemdSite.notify_variable: "@" + self.name}

    def wait(self, process: subprocess.Popen, port: int, timeout: float = 20) -> None:
        """Return once the connector reported ready and its port accepts; fail on early exit."""
        assert process.stdout is not None
        output = bytearray()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            ready.register(self.socket, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                events = ready.select(max(0, deadline - time.monotonic()))
                if not events:
                    break
                for key, _ in events:
                    if key.fileobj is self.socket:
                        assert self.socket.recv(4096) == b"READY=1"
                        with socket.socket() as bound:
                            assert bound.connect_ex(("127.0.0.1", port)) == 0
                        return
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        process.wait(timeout=5)
                        raise ServiceNotServingError(
                            f"Connector exited before binding: {output.decode(errors='replace')}"
                        )
                    output.extend(chunk)
        raise ServiceNotServingError(f"Connector did not bind: {output.decode(errors='replace')}")

    def close(self) -> None:
        self.socket.close()
