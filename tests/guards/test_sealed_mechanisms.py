"""Mechanism adaptation fails at import; declaration families stay extensible."""

import importlib
from types import ModuleType

import pytest

from agent_comms.child_process import AttachedChild, ChildProcess, SynchronousProcess
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.pending_requests import PendingRequests
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.read_ledger import ReadLedger
from agent_comms.sealed import Sealed

pytestmark = pytest.mark.refactor_guard


def test_all_mechanisms_reject_external_adapters_and_keep_owned_implementations():
    for mechanism in (FieldCodec, PendingRequests, ReadLedger, PiRpcChannel, ChildProcess):
        module = ModuleType("external_adapter")
        module.__dict__["Alias"] = mechanism
        with pytest.raises(TypeError, match=f"sealed mechanism {mechanism.__name__}"):
            exec("class Adapter(Alias): pass", module.__dict__)
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
    with pytest.raises(TypeError, match="sealed mechanism Owned"):
        exec("class Adapter(Owned): pass", adapter.__dict__)

    adapter.__dict__.update(Sealed=Sealed, Mechanism=owner.Mechanism)
    exec("class Local(Sealed): pass", adapter.__dict__)
    with pytest.raises(TypeError, match="sealed mechanism Mechanism"):
        exec("class Adapter(Local, Mechanism): pass", adapter.__dict__)
