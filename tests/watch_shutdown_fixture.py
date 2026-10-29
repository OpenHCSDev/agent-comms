"""Held phase inputs served by the real connector, signal handler and watch_worker."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from agent_comms.child_process import ReadWork
from agent_comms.comms import Comms
from agent_comms.connector import Connector, ServeVerb
from agent_comms.connector_store import ConnectorStore
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_bus import Watch, WatchPass
from agent_comms.repo import Repo
from source_revision_fixture import SourceRepository
from connector_shutdown_fixture import HeldCall


class ShutdownWatch(HeldCall, WatchPass[None], affix="ShutdownWatch"):
    """A private declaration family, never an extra production watch registration."""

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


class JoinedShutdownWatch(ReadWork, ShutdownWatch, declared_name=None):
    def commit(self, comms: Comms) -> None:
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


class WriteShutdownWatch(JoinedShutdownWatch):
    def pass_body(self, comms: Comms, observed: None) -> None:
        self.commit(comms)


class PreparationShutdownWatch(JoinedShutdownWatch):
    def prepare(self, comms: Comms) -> None:
        source = SourceRepository.create(self.directory / "source")
        tree = source.git(source.remote_path, "rev-parse", "main^{tree}").strip()
        head = source.git(source.remote_path, "commit-tree", tree, "-p", source.main_commit,
                          "-m", "Preparation must finish acquiring this object").strip()
        source.git(source.remote_path, "update-ref", "refs/heads/main", head)
        # Hold the actual external upload-pack, not our acquisition/reader implementation.
        source.git(source.checkout, "config", "remote.origin.uploadpack",
                   f"{sys.executable} -m watch_shutdown_fixture upload-pack {self.directory}")
        Repo.require_commits(source.checkout, "origin", head)
        assert source.has_commit(head)
        self.commit(comms)


@dataclass(frozen=True)
class WatchConnector(Connector):
    watches: Mapping[type[Watch], Watch] = field(kw_only=True, repr=False, compare=False)


def main() -> int:
    if sys.argv[1] == "upload-pack":
        HeldCall(directory=Path(sys.argv[2])).wait()
        return subprocess.run(("git-upload-pack", *sys.argv[3:]), check=False).returncode
    root, auth, port, selected, directory = sys.argv[1:]
    watch = ShutdownWatch.member_named(selected)(directory=Path(directory))
    production = Connector(ConnectorStore.at(Path(auth)), f"http://127.0.0.1:{port}", Path(root))
    connector = WatchConnector(production.store, production.issuer, production.root,
        watches=MappingProxyType({**production.watches, type(watch): watch}))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    with LaunchRoot.from_path(Path(root)).selected():
        asyncio.run(ServeVerb(root=Path(root), port=int(port)).serve(connector.application(), connector))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
