import subprocess
import sys
from abc import abstractmethod
from dataclasses import dataclass
from types import MappingProxyType

import pytest
from metaclass_registry import DuplicateRegistryKeyError

from agent_comms.execution.acp_failure import ACPFailure
from agent_comms.execution.native_input_record import NativeInputExecution
from agent_comms.execution.turn_failure import TurnFailure
from agent_comms.nominal.declared_family import DeclaredFamily, DeclaredOrder, RegisteredFamily
from agent_comms.planning.orders import FoundProgress
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery, HarnessObservedPart


def test_module_owned_name_lookup_imports_only_its_original_module(tmp_path):
    package = tmp_path / "module_owned_cases"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "root.py").write_text(
        "from agent_comms.nominal.declared_family import DeclaredFamily, ModuleMembers\n"
        "class Command(ModuleMembers, DeclaredFamily, affix='Case', "
        "discovery_package=__package__): pass\n"
    )
    for stem in ("selected", "unrelated"):
        (package / f"{stem}.py").write_text(
            "from module_owned_cases.root import Command\n"
            f"class {stem.title()}Case(Command): pass\n"
        )
    checked = subprocess.run(
        (sys.executable, "-c", """
import importlib
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from module_owned_cases.root import Command
from agent_comms.nominal.declared_family import UnknownFamilyMember
selected = Command.member_named('selected')
assert selected.__module__ == 'module_owned_cases.selected'
assert 'module_owned_cases.unrelated' not in sys.modules
for invalid in ('absent', 'selected.missing', '../selected', [], None):
    try:
        Command.member_named(invalid)
    except UnknownFamilyMember:
        pass
    else:
        raise AssertionError(invalid)
assert set(Command.names()) == {'selected', 'unrelated'}
try:
    selected.member_named('unrelated')
except UnknownFamilyMember:
    pass
else:
    raise AssertionError('a sibling became a selected subclass member')
Path(sys.argv[1], 'module_owned_cases', 'dependency.py').write_text(
    'import absent_command_dependency\\n')
importlib.invalidate_caches()
try:
    Command.member_named('dependency')
except ModuleNotFoundError as error:
    assert error.name == 'absent_command_dependency'
else:
    raise AssertionError('a missing required dependency was hidden')
print('selected-only lookup, complete enumeration, unknown names, subtypes, dependency errors')
""", str(tmp_path)),
        capture_output=True, text=True, timeout=45,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_registry_tombstones_decode_through_the_original_row_scope(tmp_path):
    from agent_comms.projects.checkouts import Checkout
    from agent_comms.registry.registration import Registration
    from agent_comms.registry.threads import NativeThread
    from agent_comms.storage.locked_store import RegistryFile

    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.declare(NativeThread("removed", frozenset(), Checkout(tmp_path)))
    registry.remove("removed")
    checked = subprocess.run(
        (sys.executable, "-c", """
import sys
from pathlib import Path
from agent_comms.registry.registry_store import RegistryStore
snapshot = RegistryStore.at(Path(sys.argv[1])).snapshot()
assert not snapshot.threads
assert 'agent_comms.transport.connector' not in sys.modules
""", str(tmp_path)), capture_output=True, text=True, timeout=45,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


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
    assert HTTPReadyCase.declared_position() < CustomCase.declared_position()
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


@pytest.mark.parametrize("slots", (False, True))
@pytest.mark.parametrize("frozen", (False, True))
def test_registered_family_constructs_only_declared_members(slots: bool, frozen: bool):
    class Family(RegisteredFamily, affix="Case"):
        pass

    @dataclass(slots=slots, frozen=frozen)
    class NamedCase(Family):
        value: int

    @dataclass(slots=slots, frozen=frozen)
    class UnnamedCase(Family, declared_name=None):
        value: int

    class DerivedCase(UnnamedCase):
        pass

    assert NamedCase(3).value == 3
    assert DerivedCase(4).value == 4
    for declaration, arguments in ((Family, ()), (UnnamedCase, (3,))):
        with pytest.raises(TypeError, match=declaration.__name__):
            declaration(*arguments)


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
    assert Left.family_root is Left and Right.family_root is Right
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


def test_named_lookup_accepts_a_sealed_registry():
    class Sealed(DeclaredFamily):
        pass

    class Member(Sealed):
        pass

    Sealed.__registry__ = MappingProxyType(dict(Sealed.__registry__))
    assert Sealed.member_named("member") is Member
    with pytest.raises(ValueError):
        Sealed.member_named("missing")


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


def test_ordered_family_views_have_one_declaration_module():

    for family in (TurnFailure, FoundProgress, ACPFailure, NativeInputExecution):
        assert {member.__module__ for member in family.members_with(family)} == {family.__module__}


def test_plain_order_uses_the_first_rank_in_the_mro_and_requires_a_rank():
    class Rank(DeclaredOrder):
        pass

    class First(Rank):
        pass

    class Second(Rank):
        pass

    class Selected(Second):
        pass

    class Override(First, Selected):
        pass

    assert Rank.position_of(First) < Rank.position_of(Selected)
    assert Rank.position_of(Override) == Rank.position_of(First)
    with pytest.raises(ValueError):
        Rank.position_of(object)


def test_attachment_names_decode_in_a_process_that_never_imported_their_module():
    """A heartbeat on the bus decodes before anything imports its member module."""
    probe = (
        "import sys\n"
        "from agent_comms.messaging.task_attachment import NoTaskAttachment, TaskAttachment\n"
        "from agent_comms.nominal.declared_family import UnknownFamilyMember\n"
        "from agent_comms.nominal.field_codec import FieldCodec\n"
        "assert FieldCodec.decode(TaskAttachment, {'kind': 'absent'}) == NoTaskAttachment()\n"
        "assert 'agent_comms.task_attachments.team_pings' not in sys.modules\n"
        "assert 'agent_comms.task_attachments.owner_notices' not in sys.modules\n"
        "try:\n"
        "    TaskAttachment.member_named([])\n"
        "except UnknownFamilyMember:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('unhashable name was accepted')\n"
        "assert 'agent_comms.task_attachments.team_pings' not in sys.modules\n"
        "print(TaskAttachment.member_named('team_decisions').__module__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=True,
    )

    assert result.stdout.split() == ["agent_comms.task_attachments.team_pings"]


@pytest.mark.parametrize("table", [HarnessDelivery.__name__, HarnessObservedPart.__name__])
def test_stored_schema_text_is_the_same_whichever_bridge_imports_first(table):
    """The Claude and Codex bridges register HarnessSession members in opposite orders."""
    def ddl(first: str, second: str) -> str:
        probe = (
            f"import agent_comms.cli_bridges.{first}\n"
            f"import agent_comms.cli_bridges.{second}\n"
            f"from agent_comms.coordination.tables.harness_deliveries import {table}\n"
            f"print('\\n'.join({table}.ddl()))\n"
        )
        return subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=True,
        ).stdout

    assert ddl("claude", "codex") == ddl("codex", "claude")


def test_pre_lock_acquisition_uses_the_familys_original_declaration_reader():
    acquisitions = []

    class OwnedFamily(DeclaredFamily):
        @classmethod
        def _member_declarations(cls):
            acquisitions.append(cls)
            return super()._member_declarations()

    class Member(OwnedFamily):
        pass

    OwnedFamily.acquire_declarations()
    assert acquisitions == [OwnedFamily]
    acquisitions.clear()
    assert OwnedFamily.member_named('member') is Member
    assert acquisitions == [OwnedFamily]
    with pytest.raises(ValueError):
        OwnedFamily.member_named('missing')


def test_registry_opening_acquires_only_its_rows_in_a_fresh_process():
    probe = '''
import sys
from agent_comms.registry.registry_tables import RegistryNamedRow, RegistryRows
from agent_comms.storage.typed_table import TypedTable
before = {row.declared_name: row for row in RegistryNamedRow.families()}
RegistryNamedRow.acquire_declarations()
assert {row.declared_name: row for row in RegistryNamedRow.families()} == before
assert before
assert 'agent_comms.transport.connector' not in sys.modules
assert 'agent_comms.coordinated_runtime' not in sys.modules
print(len(before))
'''
    result = subprocess.run([sys.executable, '-c', probe], capture_output=True,
                            text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0


def test_viewer_wire_families_decode_without_package_walk_or_packaging():
    probe = '''
import builtins
import sys

orig_import = builtins.__import__
def block_packaging(name, *args, **kwargs):
    if name == "packaging" or name.startswith("packaging."):
        raise ModuleNotFoundError("No module named 'packaging'", name="packaging")
    return orig_import(name, *args, **kwargs)

builtins.__import__ = block_packaging

from agent_comms.coordination.tables.assignments import WakeAssignment
from agent_comms.planning.goal_ledger import FailedTurnEvidence
from agent_comms.ui_model.teammate_state import TeammateState

assert FailedTurnEvidence.recorded_member(FailedTurnEvidence.declared_name) is FailedTurnEvidence
assert WakeAssignment.recorded_member(WakeAssignment.declared_name) is WakeAssignment
for name in ("idle", "blocked", "moving", "spinning"):
    assert issubclass(TeammateState.recorded_member(name), TeammateState)

for forbidden in (
    "packaging",
    "agent_comms.cli_verbs.install",
    "agent_comms.installer.launch",
    "agent_comms.team.standing",
):
    assert forbidden not in sys.modules, f"Unexpectedly imported {forbidden}"
print("ok")
'''
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok"
