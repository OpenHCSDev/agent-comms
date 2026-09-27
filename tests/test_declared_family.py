from abc import abstractmethod
from dataclasses import dataclass

import pytest

from agent_comms.declared_family import DeclaredFamily


class SampleFamily(DeclaredFamily, affix="Case"):
    @abstractmethod
    def execute(self): ...


class Capability:
    pass


@dataclass(frozen=True, slots=True)
class HTTPReadyCase(SampleFamily, Capability):
    value: int

    def execute(self):
        return self.value


class AbstractCase(SampleFamily):
    pass


class CustomCase(SampleFamily, declared_name="external-v1"):
    def execute(self):
        return 1


def test_golden_names_decode_capability_and_slots_identity():
    assert SampleFamily.names() == ("http_ready", "external-v1")
    assert SampleFamily.decode("http_ready") is HTTPReadyCase
    assert SampleFamily.members_with(Capability) == (HTTPReadyCase,)
    assert HTTPReadyCase(3).execute() == 3
    with pytest.raises(TypeError):
        AbstractCase()
    with pytest.raises(ValueError):
        SampleFamily.decode("missing")
    with pytest.raises(ValueError):
        HTTPReadyCase.decode("external-v1")


def test_collisions_fail_without_replacing_owner():
    with pytest.raises(TypeError, match="Duplicate"):

        class Collision(SampleFamily, declared_name="http_ready"):
            def execute(self):
                return 0

    assert SampleFamily.decode("http_ready") is HTTPReadyCase


def test_independent_families_and_inherited_names_are_derived():
    class Other(DeclaredFamily):
        pass

    class HTTPReady(Other):
        pass

    class Child(HTTPReady):
        pass

    assert Other.names() == ("http_ready", "child")
    assert Other.decode("http_ready") is HTTPReady
    assert Child.declared_name == "child"
    with pytest.raises(TypeError, match="two families"):

        class Cross(HTTPReady, CustomCase):
            pass
