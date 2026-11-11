"""Mechanism adaptation fails at import; declaration families stay extensible."""

import importlib
from types import ModuleType

import pytest

from agent_comms.execution.child_process import (
    AttachedChild,
    ChildProcess,
    SynchronousProcess,
)
from agent_comms.execution.pending_requests import PendingRequests
from agent_comms.execution.pi_rpc import PiRpcChannel
from agent_comms.messaging.read_ledger import ReadLedger
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.nominal.sealed import Sealed

pytestmark = pytest.mark.refactor_guard


def test_all_mechanisms_reject_external_adapters_and_keep_owned_implementations():
    for mechanism in (FieldCodec, PendingRequests, ReadLedger, PiRpcChannel, ChildProcess):
        namespace: dict[str, object] = {"__module__": "external_adapter"}
        with pytest.raises(TypeError):
            type("Adapter", (mechanism,), namespace)
        owner = importlib.import_module(mechanism.__module__)
        for value in vars(owner).values():
            if isinstance(value, type) and issubclass(value, mechanism):
                assert value.__module__ == mechanism.__module__
    assert issubclass(AttachedChild, ChildProcess)
    assert issubclass(SynchronousProcess, ChildProcess)

    class NewCase(DeclaredFamily):
        pass

    class ExtraCase(NewCase):
        pass

    assert ExtraCase in NewCase.members_with(NewCase)


def test_seal_follows_owned_descendants_and_every_mechanism_in_multiple_inheritance():
    owner = ModuleType("mechanism_owner")
    owner.__dict__["Sealed"] = Sealed
    exec("class Mechanism(Sealed): pass\nclass Owned(Mechanism): pass", owner.__dict__)
    adapter = ModuleType("external_adapter")
    adapter.__dict__["Owned"] = owner.Owned
    with pytest.raises(TypeError):
        exec("class Adapter(Owned): pass", adapter.__dict__)

    adapter.__dict__.update(Sealed=Sealed, Mechanism=owner.Mechanism)
    exec("class Local(Sealed): pass", adapter.__dict__)
    with pytest.raises(TypeError):
        exec("class Adapter(Local, Mechanism): pass", adapter.__dict__)
