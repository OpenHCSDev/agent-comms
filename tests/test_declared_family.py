from abc import abstractmethod
from dataclasses import dataclass

import pytest
from metaclass_registry import DuplicateRegistryKeyError

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
    assert SampleFamily.member_named("http_ready") is HTTPReadyCase
    assert SampleFamily.members_with(Capability) == (HTTPReadyCase,)
    assert HTTPReadyCase(3).execute() == 3
    with pytest.raises(TypeError):
        AbstractCase()
    with pytest.raises(ValueError):
        SampleFamily.member_named("missing")
    with pytest.raises(ValueError):
        HTTPReadyCase.member_named("external-v1")


def test_explicit_unnamed_concrete_state_uses_existing_registry_exclusion():
    class UnnamedCase(SampleFamily):
        declared_name = None

        def execute(self):
            return 2

    assert UnnamedCase().execute() == 2
    assert SampleFamily.names() == ("http_ready", "external-v1")
    with pytest.raises(ValueError):
        SampleFamily.member_named("unnamed")


def test_slots_root_keeps_its_declared_affix():
    @dataclass(frozen=True, slots=True)
    class Limits(DeclaredFamily, affix="Limit"):
        value: int

        @abstractmethod
        def amount(self): ...

    class ByteLimit(Limits):
        def amount(self):
            return self.value

    assert Limits.names() == ("byte",)
    assert Limits.member_named("byte") is ByteLimit
    assert ByteLimit(4).amount() == 4


def test_child_roots_share_behavior_not_membership_and_survive_slots():
    class Behavior(DeclaredFamily):
        def answer(self):
            return 7

    @dataclass(frozen=True, slots=True)
    class Left(Behavior, affix="Item"):
        pass

    @dataclass(frozen=True, slots=True)
    class Right(Behavior, affix="Item"):
        pass

    def declare(root):
        @dataclass(frozen=True, slots=True)
        class ValueItem(root):
            pass
        return ValueItem

    left, right = declare(Left), declare(Right)
    assert Behavior.members_with(Behavior) == ()
    assert Left._family_root is Left and Right._family_root is Right
    assert Left.members_with(Left) == (left,)
    assert Right.members_with(Right) == (right,)
    assert Left.member_named("value") is left and Right.member_named("value") is right
    assert left().answer() == right().answer() == 7


def test_collisions_fail_without_replacing_owner():
    with pytest.raises(DuplicateRegistryKeyError):

        class Collision(SampleFamily, declared_name="http_ready"):
            def execute(self):
                return 0

    assert SampleFamily.member_named("http_ready") is HTTPReadyCase


def test_independent_families_and_inherited_names_are_derived():
    class Other(DeclaredFamily):
        pass

    class HTTPReady(Other):
        pass

    class Child(HTTPReady):
        pass

    assert Other.names() == ("http_ready", "child")
    assert Other.member_named("http_ready") is HTTPReady
    assert Child.declared_name == "child"
    with pytest.raises(TypeError):

        class Cross(HTTPReady, CustomCase):
            pass
