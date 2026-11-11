"""An inbox read runs on every socket's wake, so it reads no clock or thread row it does not need."""
import pytest

from agent_comms.errors import UnknownChannelError
from agent_comms.messaging.bus_activity_index import BusActivityIndex
from agent_comms.messaging.channels import RetiredTagChannel
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import StoredThreadRow
from agent_comms.registry.threads import ExternalThread
from conftest import installed_comms


def forbid(owner: object, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(f"{name} ran on an inbox read")

    monkeypatch.setattr(owner, name, forbidden)


def test_an_inbox_read_reads_only_its_own_channel_clock(tmp_path, monkeypatch):
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(ExternalThread("reader", frozenset({"api"}), Checkout(comms.root)))
    comms.registry.declare(ExternalThread("writer", frozenset({"api"}), Checkout(comms.root)))
    comms.messaging.send("writer", "#api", "hello")

    forbid(BusActivityIndex, "clocks", monkeypatch)
    assert len(comms.bus.inbox("reader").page.messages) == 1


def test_a_channel_with_only_activity_left_still_resolves_from_its_own_row(tmp_path, monkeypatch):
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(ExternalThread("writer", frozenset({"api"}), Checkout(comms.root)))
    comms.messaging.send("writer", "#api", "hello")
    comms.channels.delete_tag("api")

    forbid(BusActivityIndex, "clocks", monkeypatch)
    assert isinstance(comms.bus.require_channel("api"), RetiredTagChannel)
    with pytest.raises(UnknownChannelError):
        comms.bus.require_channel("#never-used")


def test_sender_bindings_decode_no_thread_row(tmp_path, monkeypatch):
    comms = installed_comms(tmp_path / "wire")
    for name in ("alpha", "beta"):
        comms.registry.declare(ExternalThread(name, frozenset(), Checkout(comms.root)))
    comms.registry.rename("beta", "beta-renamed")
    expected = comms.registry.snapshot().sender_bindings

    forbid(StoredThreadRow.thread, "func", monkeypatch)
    assert Registration(comms.registry.store.path).snapshot().sender_bindings == expected
