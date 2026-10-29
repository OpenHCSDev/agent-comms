"""Held phase inputs served by the real connector, signal handler and watch_worker."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.child_process import ReadWork
from agent_comms.comms import Comms
from agent_comms.connector import Connector, ServeVerb
from agent_comms.connector_store import ConnectorStore
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_bus import Watch, WatchPass
from connector_shutdown_fixture import HeldCall


class ShutdownWatch(HeldCall, WatchPass[None, None], affix="ShutdownWatch"):
    """A private declaration family, never an extra production watch registration."""

    def prepare(self, comms: Comms) -> None:
        pass

    def observe(self, comms: Comms, prepared: None) -> None:
        pass

    def pass_body(self, comms: Comms, observed: None) -> None:
        pass

    @classmethod
    async def release_on_stop(cls, process: subprocess.Popen[bytes], directory: Path) -> None:
        """Readers receive no release before their connector exits."""

    @classmethod
    def check_exit(cls, comms: Comms, directory: Path, elapsed: float) -> None:
        assert elapsed < 1, f"Read observation held shutdown for {elapsed:.3f}s"
        assert not (directory / "finished").exists()
        assert not (directory / "release").exists()


class ReadShutdownWatch(ReadWork, ShutdownWatch):
    def observe(self, comms: Comms, prepared: None) -> None:
        self.wait()
        (self.directory / "finished").touch()


class WriteShutdownWatch(ShutdownWatch):
    def pass_body(self, comms: Comms, observed: None) -> None:
        self.wait()
        comms.messaging.send("claude-ai", "worker", "watch write committed")
        (self.directory / "finished").touch()

    @classmethod
    async def release_on_stop(cls, process: subprocess.Popen[bytes], directory: Path) -> None:
        await asyncio.sleep(1.2)
        assert process.poll() is None, "The entered write phase was abandoned or capped"
        (directory / "release").touch()

    @classmethod
    def check_exit(cls, comms: Comms, directory: Path, elapsed: float) -> None:
        assert elapsed >= 1.2
        assert (directory / "finished").exists()
        assert any(message.body == "watch write committed"
                   for message in comms.bus.inbox("worker").page.messages)


@dataclass(frozen=True)
class WatchConnector(Connector):
    watches: Mapping[type[Watch], Watch] = field(default_factory=dict)


def main() -> int:
    root, auth, port, selected, directory = sys.argv[1:]
    watch = ShutdownWatch.member_named(selected)(directory=Path(directory))
    connector = WatchConnector(ConnectorStore.at(Path(auth)), f"http://127.0.0.1:{port}",
                               Path(root), watches={type(watch): watch})
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    with LaunchRoot.from_path(Path(root)).selected():
        asyncio.run(ServeVerb(root=Path(root), port=int(port)).serve(connector.application(), connector))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
