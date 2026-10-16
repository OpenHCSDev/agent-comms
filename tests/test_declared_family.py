import subprocess
import sys
import textwrap
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import import_module

import pytest
from metaclass_registry import DuplicateRegistryKeyError
from metaclass_registry import core as registry_core
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec


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


def test_explicit_unnamed_concrete_state_uses_existing_registry_exclusion():
    class UnnamedCase(SampleFamily):
        declared_name = None

        def execute(self):
            return 2

    assert UnnamedCase().execute() == 2
    assert SampleFamily.names() == ("http_ready", "external-v1")
    with pytest.raises(ValueError):
        SampleFamily.decode("unnamed")


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
    assert Limits.decode("byte") is ByteLimit
    assert ByteLimit(4).amount() == 4


def test_collisions_fail_without_replacing_owner():
    with pytest.raises(DuplicateRegistryKeyError, match="http_ready"):

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


@pytest.mark.parametrize('layout', ['module', 'package'])
def test_declared_scope_builds_once_for_module_and_package(tmp_path, monkeypatch, layout):
    root_name = 'declared_scope_root_' + layout
    member_name = 'declared_scope_members_' + layout
    (tmp_path / (root_name + '.py')).write_text(
        'from agent_comms.declared_family import DeclaredFamily\n'
        f'class Scope(DeclaredFamily, affix="Case", discovery_package={member_name!r}):\n'
        '    pass\n'
    )
    member_text = f'from {root_name} import Scope\nclass NativeCase(Scope):\n    pass\n'
    if layout == 'module':
        (tmp_path / (member_name + '.py')).write_text(member_text)
    else:
        package = tmp_path / member_name
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / 'native.py').write_text(member_text)
    monkeypatch.syspath_prepend(str(tmp_path))
    root = import_module(root_name).Scope
    assert member_name not in sys.modules
    imports = []
    import_scope = registry_core.import_module_preserving_root_logging

    def acquire(name):
        imports.append(name)
        return import_scope(name)

    monkeypatch.setattr(registry_core, 'import_module_preserving_root_logging', acquire)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(root.decode, 'native') for _ in range(2)]
        members = [result.result() for result in results]
    assert members[0] is members[1]
    assert root.names() == ('native',)
    assert root.members_with(root) == (members[0],)
    assert FieldCodec.decode(type[root], 'native') is members[0]
    assert imports == [member_name]


def test_discovery_failure_propagates_without_marking_members_complete(monkeypatch):
    class FailingFamily(DeclaredFamily, discovery_package='missing_declared_family_scope'):
        pass

    def fail(name):
        raise ImportError(name)

    monkeypatch.setattr(registry_core, 'import_module_preserving_root_logging', fail)
    with pytest.raises(ImportError, match='missing_declared_family_scope'):
        FailingFamily.names()
    assert not FailingFamily.__registry__._discovered
    assert dict.__len__(FailingFamily.__registry__) == 0


def test_checkpoint_schema_query_stays_local_but_external_names_still_discover():
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import os, sqlite3, sys, tempfile
            from agent_comms import private_bus_checkpoint as checkpoint
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
            row = TypedTable.decode('native_runtime_input')
            assert row.declared_name == 'native_runtime_input'
        """)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
