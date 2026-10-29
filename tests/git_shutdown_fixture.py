"""Real smart-HTTP Git responses held by the remote, never by a patched Git reader."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from agent_comms.child_process import SpawnedProcess


def pipe_holder() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print("pipe holder ready", flush=True)
    time.sleep(60)


def exited_leader() -> None:
    # Deliberately inherit the leader's stdout pipe and process group.
    subprocess.Popen((sys.executable, "-c",
                      "from git_shutdown_fixture import pipe_holder; pipe_holder()"))


class GitHTTPHandler(BaseHTTPRequestHandler):
    server: HeldGitHTTP

    def do_GET(self) -> None:
        self.exchange()

    def do_POST(self) -> None:
        self.exchange()

    def exchange(self) -> None:
        address = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        environment = dict(os.environ, GIT_PROJECT_ROOT=str(self.server.remote.parent),
                           GIT_HTTP_EXPORT_ALL="1", REQUEST_METHOD=self.command,
                           PATH_INFO=address.path, QUERY_STRING=address.query,
                           CONTENT_TYPE=self.headers.get("Content-Type", ""),
                           CONTENT_LENGTH=str(length), REMOTE_USER="private-control")
        protocol = self.headers.get("Git-Protocol")
        if protocol is not None:
            environment["HTTP_GIT_PROTOCOL"] = protocol
        with SpawnedProcess.launch(
            ("git", "http-backend"), cwd=self.server.remote.parent, env=environment,
            stdin=subprocess.PIPE, output=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as backend:
            response, error = backend.process.communicate(self.rfile.read(length), timeout=10)
            if backend.process.wait():
                raise RuntimeError(error.decode(errors="replace"))
        header, _separator, body = response.partition(b"\r\n\r\n")
        self.server.requests.append((self.command, address.path))
        if self.command == self.server.hold_method:
            self.server.entered.set()
            self.server.on_enter()
            if not self.server.release.wait(10):
                raise TimeoutError("Private Git response was never released")
        fields = tuple(line.decode().partition(": ")[::2] for line in header.split(b"\r\n"))
        status = next((int(value.split()[0]) for name, value in fields if name == "Status"), 200)
        with suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(status)
            for name, value in fields:
                if name != "Status":
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class HeldGitHTTP(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, remote: Path, hold_method: str,
                 on_enter: Callable[[], None] = lambda: None) -> None:
        super().__init__(("127.0.0.1", 0), GitHTTPHandler)
        self.remote = remote
        self.hold_method = hold_method
        self.on_enter = on_enter
        self.entered = threading.Event()
        self.release = threading.Event()
        self.requests: list[tuple[str, str]] = []

    @property
    def url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}/{self.remote.name}"

    @classmethod
    @contextmanager
    def serving(cls, remote: Path, hold_method: str,
                on_enter: Callable[[], None] = lambda: None) -> Iterator[HeldGitHTTP]:
        with cls(remote, hold_method, on_enter) as server:
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05},
                                      daemon=True)
            worker.start()
            try:
                yield server
            finally:
                server.release.set()
                server.shutdown()
                worker.join(timeout=5)
                if worker.is_alive():
                    raise TimeoutError("Private HTTP server did not retire")
