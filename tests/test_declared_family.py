import subprocess
import sys
import textwrap
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
    with pytest.raises(DuplicateRegistryKeyError, match="http_ready"):

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
    with pytest.raises(TypeError, match="two families"):

        class Cross(HTTPReady, CustomCase):
            pass


def test_checkpoint_schema_query_stays_local_but_external_names_still_discover():
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import os, sqlite3, sys, tempfile
            import agent_comms.private_bus_checkpoint as checkpoint
            from agent_comms.typed_table import TypedTable

            # A real checkpoint schema can be installed and decoded with only
            # its owner's declarations, without loading ACP under a bus lock.
            tables = TypedTable.members_with(checkpoint.CheckpointTable)
            assert checkpoint.PrefixCertificate in tables
            with sqlite3.connect(':memory:') as db, tempfile.TemporaryFile() as source:
                for table in tables:
                    table.create(db)
                certificate = checkpoint.PrefixCertificate.capture(
                    '0' * 32, os.fstat(source.fileno()), 0, bytes(32), '0' * 64)
                certificate.insert(db)
                assert checkpoint._saved(db) == certificate.witness()
            assert 'agent_comms.acp' not in sys.modules

            # Schema-report's external-name boundary still acquires the full
            # distributed family; this change must not hide unloaded tables.
            row = TypedTable.member_named('native_runtime_input')
            assert row.declared_name == 'native_runtime_input'
        """)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
