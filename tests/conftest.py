"""Shared fixtures: an isolated Comms wire per test."""
from agent_comms.native_package import pinned_location, pinned_native_package

import asyncio
import os
import subprocess
import sys
import tempfile
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from xdist import is_xdist_worker

from agent_comms.bus_publication import AgentOrigin
from agent_comms.comms import Comms
from agent_comms.message_bus import MessageBus
from agent_comms.native_tool_transport import ToolListener
from agent_comms.publisher import ParticipantPublisher, Publisher
from agent_comms.registration import Registration
from agent_comms.messages import ConversationRole, InfoKind, Message
from agent_comms.store_files import _store_lock


class TranscriptRecords:
    """Fixed test time, projected through each record's declared constructor."""

    def __init__(self):
        from agent_comms.field_codec import RecordedTimestamp

        self.timestamp = RecordedTimestamp("2026-10-01T00:00:00.000Z")

    def event(self, declaration, *args, **facts):
        return declaration(*args, timestamp=self.timestamp.at.timestamp(), **facts)

    def entry(self, declaration, **facts):
        return declaration(timestamp=self.timestamp, **facts)

    def message(self, payload, **coordinates):
        from agent_comms.native_entries import MessageEntry
        from agent_comms.pi_payloads import PiMessage

        entry = self.entry(MessageEntry, message=PiMessage.from_wire(payload), **coordinates)
        return {**entry.to_wire(), "message": payload}

    def session(self, **coordinates):
        from agent_comms.native_entries import SessionEntry

        return self.entry(SessionEntry, **coordinates)


transcript_records = TranscriptRecords()


_MANAGED_RUNTIME_ENVIRONMENT = frozenset({
    "AGENT_COMMS_MANAGED",
    "AGENT_COMMS_PROJECT_REQUEST",
    "AGENT_COMMS_PROJECT_SOCKET",
    "AGENT_COMMS_ROOT",
    "AGENT_COMMS_THREAD",
    "PI_PARENT_ID",
    "PI_SESSION_FILE",
    "PI_SESSION_ID",
    "PI_TASK",
    "PI_WORKTREE",
})


async def await_event_or_task(event: Awaitable[object], task: asyncio.Task) -> object:
    """A producer failure ends its event wait with the original exception."""
    observed = asyncio.ensure_future(event)
    try:
        done, _ = await asyncio.wait((observed, task), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert observed.done(), 'Producer finished without the expected event'
        return observed.result()
    finally:
        observed.cancel()
        await asyncio.gather(observed, return_exceptions=True)


def reader(root: Path) -> Comms:
    """A Comms over an existing wire that makes no private write."""
    return Comms(root, Publisher)


def responder(root: Path, registry: Registration) -> MessageBus[ParticipantPublisher]:
    """The selected owner publishes replies and releases its attempt's claims."""
    return MessageBus(root / "bus.jsonl", registry, ParticipantPublisher)


def publish_initial(comms, sender, target, body, type=InfoKind, *, role=ConversationRole()):
    """Stage an agent's initial message on the bus; no SQL acceptance or live wake."""
    type.require_sendable()
    with _store_lock(comms.root / "wire"):
        return comms.bus.publisher.publish_initial_cohort(
            Message(sender=sender, target=target, body=body, type=type, role=role),
            origin=AgentOrigin(),
        )

def pytest_sessionstart(session):
    """The controller derives this checkout's pinned native package before any worker runs.

    prepare-pi-native keys its build by the pin and reuses a verified one, so an
    unchanged pin costs nothing and a changed pin rebuilds without anyone remembering.
    """
    if is_xdist_worker(session) or pinned_location().is_dir():
        return
    root = Path(__file__).resolve().parents[1]
    subprocess.run([str(root / "stack/bin/prepare-pi-native"), "--python", sys.executable], check=True)


@pytest.fixture
async def tool_listener():
    """Every private test endpoint has one joined retirement owner."""
    async with AsyncExitStack() as resources:
        def create(directory, token):
            listener = ToolListener(directory, token)
            resources.push_async_callback(listener.close)
            return listener
        yield create


@pytest.fixture
def source_reads():
    from agent_comms.native_entries import NativeEvidenceScope

    with NativeEvidenceScope() as scope:
        yield scope


@pytest.fixture
def cursor_delivery(source_reads):
    from agent_comms.cursor_publication import CursorDelivery

    return CursorDelivery(source_reads=source_reads)


@pytest.fixture(scope="session")
def native_runtime_package():
    """The artifact of this checkout's own pin, never the installed package's."""
    from agent_comms.native_package import VerifiedNativePackage

    return VerifiedNativePackage.acquire(pinned_native_package())


@pytest.fixture(autouse=True)
def _managed_runtime_environment_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's live managed process is not a test wire or native child."""
    from agent_comms.launch_root import LaunchRoot

    monkeypatch.setattr(LaunchRoot, "_current", LaunchRoot.unstarted())
    for name in _MANAGED_RUNTIME_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def private_directory():
    """A throwaway directory on physical /var/tmp; private N/K publication refuses /tmp."""
    if (
        os.name != "posix"
        or not Path("/var/tmp").is_dir()
        or Path("/var").is_symlink()
        or Path("/var/tmp").is_symlink()
    ):
        pytest.skip("a private bus needs a physical /var/tmp")
    with tempfile.TemporaryDirectory(prefix="ac-private-", dir="/var/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def private_comms(private_directory):
    """One throwaway private bus, built as production builds one.

    Its launch derives like any private root's: the marker installed here
    gives the root ID, this installation the package.
    """
    root = private_directory / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.messaging.initialize_private_initial_protocol()
    from agent_comms.launch_root import LaunchRoot

    LaunchRoot.from_path(root).install()
    comms.route.launch().require_private().check()
    return comms


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "comms"


def declare_test_bus(comms: Comms, *, channels=(), threads=()) -> Comms:
    """Declare exactly the test's named channels and members, never inferred ones."""
    for channel in channels:
        comms.channels.create_tag(channel)
    for thread in threads:
        comms.registry.declare(thread)
    return comms


@pytest.fixture
def comms(root: Path) -> Comms:
    return Comms(root)


@pytest.fixture
def thread_registration(tmp_path_factory):
    from agent_comms.registration import Registration
    return Registration(tmp_path_factory.mktemp('thread-registration') / 'registry.json')


@pytest.fixture
def wired(comms: Comms) -> Comms:
    """A wire with PR111 (parent) and fixer (child) registered."""
    return declare_test_bus(comms, channels=("base", "auth"), threads=(
        comms.registry.new_thread(name='PR111', tags=frozenset({'base'}), worktree='/tmp/wt1'),
        comms.registry.new_thread(name='fixer', tags=frozenset({'auth'}), worktree='/tmp/wt1', parent='PR111', task='fix auth'),
    ))


