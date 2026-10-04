"""Exercise the command against real Git commits, without mocking Git or ASTs."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard



class Repository:
    def __init__(self, path: Path, root: str):
        self.path = path
        self.root = root
        self.git("init", "-q")
        self.git("config", "user.email", "ratchet@example.invalid")
        self.git("config", "user.name", "Ratchet test")

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.path), *args], text=True, timeout=10
        ).strip()

    def commit(self, files: dict[str, str | None]) -> str:
        for name, source in files.items():
            path = self.path / self.root / name
            if source is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
        self.git("add", "--all")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "test", "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def compare(self, base: str, head: str) -> tuple[int, dict]:
        result = subprocess.run(
            [str(Path(sys.executable).with_name("agent-comms-ratchet")), "--root", self.root, "--base", base, "--head", head],
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode in (0, 1), result.stderr
        return result.returncode, json.loads(result.stdout)


@pytest.fixture(params=["src/agent_comms", "src/toad"])
def repo(tmp_path: Path, request) -> Repository:
    return Repository(tmp_path, request.param)


def test_added_type_check_fails(repo: Repository) -> None:
    base = repo.commit({"check.py": 'value = row["a"]\n'})
    head = repo.commit({"check.py": "value = type(x) is int\n"})
    status, report = repo.compare(base, head)
    assert status == 1  # A different measure's reduction cannot cancel this increase.
    assert {key: report["delta"][key] for key in ("TypeIdentity", "LongBooleanChain", "StringSubscript")} == {
        "TypeIdentity": 1,
        "LongBooleanChain": 0,
        "StringSubscript": -1,
    }


def test_moves_between_files_pass(repo: Repository) -> None:
    source = 'value = type(x) is not int\nread = row["key"]\n'
    base = repo.commit({"from.py": source, "other.py": "a = 1\n"})
    moved = repo.commit({"from.py": None, "nested/to.py": source})
    combined = repo.commit({"nested/to.py": None, "other.py": "a = 1\n" + source})
    for head in (moved, combined):
        status, report = repo.compare(base, head)
        assert status == 0
        assert not any(report["delta"].values())
        assert report["head"]["TypeIdentity"] == 1
        assert report["head"]["StringSubscript"] == 1



def test_reduction_passes(repo: Repository) -> None:
    base = repo.commit({"removed.py": 'value = type(x) is int and a and b and row["key"]\n'})
    head = repo.commit({"removed.py": None})
    status, report = repo.compare(base, head)
    assert status == 0
    assert {key: report["delta"][key] for key in ("TypeIdentity", "LongBooleanChain", "StringSubscript")} == {
        "TypeIdentity": -1,
        "LongBooleanChain": -1,
        "StringSubscript": -1,
    }


def test_new_file_smells_count(repo: Repository) -> None:
    base = repo.commit({})
    head = repo.commit({"new.py": """
value = type(x) is int is not type(y)
other = type(z) == int
chain = a or b or c or d
short = a and b and c
read = row["key"]
row['other'] = read
numeric = row[0]
text = 'type(x) is int and a and b and row["not code"]'
# type(x) is int and a and b and row["not code"]
"""})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["base"] == {name: 0 for name in report["head"]}
    assert {key: report["head"][key] for key in ("TypeIdentity", "LongBooleanChain", "StringSubscript")} == {
        "TypeIdentity": 2,
        "LongBooleanChain": 1,
        "StringSubscript": 2,
    }


def test_other_source_root_does_not_enter_report(repo: Repository) -> None:
    base = repo.commit({"kept.py": "value = 1\n"})
    outside = repo.path / "unrelated.py"
    outside.write_text('value = type(x) is int and a and b and row["key"]\n')
    head = repo.commit({"kept.py": "value = 2\n"})
    status, report = repo.compare(base, head)
    assert status == 0
    assert report["paths"] == [f"{repo.root}/kept.py"]


def test_ratchet_has_one_packaged_owner() -> None:
    from agent_comms import debt_ratchet
    assert not (Path(__file__).resolve().parents[2] / "tools/debt_ratchet.py").exists()
    assert "src/agent_comms/" not in Path(debt_ratchet.__file__).read_text()


def test_builtin_handler_measure_uses_shared_collector_and_rejects_growth(repo: Repository) -> None:
    from agent_comms.debt_ratchet import BuiltinHandlerTypeSwitch
    from refactor_audit.handler_declarations import BuiltinHandlerDeclarations
    assert issubclass(BuiltinHandlerTypeSwitch, BuiltinHandlerDeclarations)
    source = '''from .mro_dispatch import MroDispatch, handles as cases
class Consumer(MroDispatch):
    @cases(dict)
    def one(self, value): pass
    @cases(DomainRecord)
    def domain(self, value): pass
'''
    base = repo.commit({"policy.py": source})
    growing = source.replace('@cases(DomainRecord)', '@cases(list, str)')
    head = repo.commit({"policy.py": growing})
    status, report = repo.compare(base, head)
    identity = f"BuiltinHandlerTypeSwitch:{repo.root}/policy.py"
    assert status == 1
    assert report["base"][identity] == 1 and report["head"][identity] == 3
    assert report["delta"][identity] == 2
    moved = repo.commit({"policy.py": source, "another_codec.py": growing,
                         "field_codec.py": growing})
    status, report = repo.compare(head, moved)
    assert status == 1  # A filename suffix cannot admit another primitive dispatcher.
    assert report["head"][f"BuiltinHandlerTypeSwitch:{repo.root}/field_codec.py"] == 0
    assert report["delta"][f"BuiltinHandlerTypeSwitch:{repo.root}/another_codec.py"] == 3


def owner(name: str, lines: int, indent: str = "") -> str:
    return indent + f"class {name}:\n" + "".join(
        indent + f"    field_{index} = {index}\n" for index in range(lines - 1)
    )


def test_family_flattening_cannot_offset_another_file_or_admit_a_named_facade(repo: Repository):
    from agent_comms.debt_ratchet import FamilyFlattened
    from refactor_audit.measures import FamilyFlattened as AuditFamilyFlattened

    original = "def carry(value):\n    return (value.declared_name, value.family_name)\n"
    base = repo.commit({"first.py": original, "second.py": "value = 1\n"})
    head = repo.commit({"first.py": "def carry(value):\n    return value\n",
                        "second.py": "def carry(value):\n    return value.declared_name\n"})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"FamilyFlattened:{repo.root}/first.py"] == -2
    assert report["delta"][f"FamilyFlattened:{repo.root}/second.py"] == 1
    assert FamilyFlattened.occurrences(ast.parse(original).body[0].body[0]) == (
        AuditFamilyFlattened.count(ast.parse(original).body[0].body[0])
    )
    mechanism = next(name for package, name in FamilyFlattened.mechanism_modules
                     if package == Path(repo.root).name)
    moved = repo.commit({"second.py": "def carry(value):\n    return value\n",
                         "another_codec.py": original, mechanism: original,
                         f"nested/{mechanism}": original})
    status, report = repo.compare(head, moved)
    assert status == 1
    assert report["delta"][f"FamilyFlattened:{repo.root}/another_codec.py"] == 2
    assert report["head"][f"FamilyFlattened:{repo.root}/{mechanism}"] == 0
    assert report["head"][f"FamilyFlattened:{repo.root}/nested/{mechanism}"] == 2


def test_small_owner_growth_and_exact_threshold_pass(repo: Repository) -> None:
    base = repo.commit({"owners.py": owner("Small", 2)})
    head = repo.commit({"owners.py": owner("Small", 500) + owner("New", 500)})
    status, report = repo.compare(base, head)
    assert status == 0
    assert not any(report["delta"].values())


def test_class_growth_cannot_be_cancelled_by_another_class(repo: Repository) -> None:
    base = repo.commit({"classes.py": owner("Growing", 501) + owner("Shrinking", 600)})
    head = repo.commit({"classes.py": owner("Growing", 502) + owner("Shrinking", 500)})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"GodClassExcess:{repo.root}/classes.py::Growing"] == 1
    assert report["delta"][f"GodClassExcess:{repo.root}/classes.py::Shrinking"] == -100


@pytest.mark.parametrize("before", [0, 500])
def test_new_or_crossing_god_class_is_refused(repo: Repository, before: int) -> None:
    base = repo.commit({"owners.py": owner("New", before)} if before else {})
    head = repo.commit({"owners.py": owner("New", 501)})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["base"][f"GodClassExcess:{repo.root}/owners.py::New"] == 0
    assert report["delta"][f"GodClassExcess:{repo.root}/owners.py::New"] == 1


def test_class_move_keeps_baseline_and_new_small_owner_is_allowed(repo: Repository) -> None:
    base = repo.commit({"old.py": owner("Existing", 501)})
    moved = repo.commit({"old.py": None, "new.py": owner("Existing", 501) + owner("NewOwner", 25)})
    status, report = repo.compare(base, moved)
    assert status == 0
    assert report["base"][f"GodClassExcess:{repo.root}/new.py::Existing"] == 1
    assert report["delta"][f"GodClassExcess:{repo.root}/new.py::Existing"] == 0
    assert report["delta"][f"GodClassExcess:{repo.root}/new.py::NewOwner"] == 0
    grown = repo.commit({"new.py": owner("Existing", 502) + owner("NewOwner", 26)})
    assert repo.compare(moved, grown)[0] == 1


def test_moved_class_growth_is_rejected(repo: Repository) -> None:
    base = repo.commit({"old.py": owner("Existing", 500)})
    head = repo.commit({"old.py": None, "new.py": owner("Existing", 501)})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"GodClassExcess:{repo.root}/new.py::Existing"] == 1


def test_nested_and_duplicate_named_classes_keep_separate_baselines(repo: Repository) -> None:
    nested = "class Outer:\n" + owner("Inner", 501, "    ")
    base = repo.commit({"one.py": nested, "two.py": owner("Inner", 500)})
    head = repo.commit({"one.py": nested, "two.py": owner("Inner", 501)})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"GodClassExcess:{repo.root}/one.py::Outer.Inner"] == 0
    assert report["delta"][f"GodClassExcess:{repo.root}/two.py::Inner"] == 1


def test_chain_terms_catch_growth_hidden_by_unchanged_chain_count(repo: Repository) -> None:
    base = repo.commit({"rules.py": "valid = a and b and c and d\n"})
    head = repo.commit({"rules.py": "valid = a and b and c and d and e\n"})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"]["LongBooleanChain"] == 0
    assert report["delta"][f"BooleanChainTerms:{repo.root}/rules.py"] == 1


@pytest.mark.parametrize(("measure", "decision"), [
    ("StringDispatch", 'if value == {case!r}: return 1'),
    ("StringDispatch", 'if value in ({case!r},): return 1'),
    ("StringDispatch", 'match value:\n        case {case!r}: return 1'),
    ("TypeSwitch", 'if isinstance(value, Case{case}): return 1'),
    ("TypeSwitch", 'if type(value) is Case{case}: return 1'),
    ("TypeSwitch", 'match value:\n        case Case{case}(): return 1'),
])
def test_dispatch_growth_and_reduction_use_the_installed_command(
    repo: Repository, measure: str, decision: str
) -> None:
    def source(cases):
        return 'def decide(value):\n' + '\n'.join(
            '    ' + decision.format(case=case) for case in cases
        ) + '\n'

    before = repo.commit({"policy.py": source(range(3))})
    grown = repo.commit({"policy.py": source(range(4))})
    status, report = repo.compare(before, grown)
    assert status == 1
    assert report["delta"][f"{measure}:{repo.root}/policy.py"] == 0
    assert report["delta"][f"{measure}Arms:{repo.root}/policy.py"] == 1
    reduced = repo.commit({"policy.py": source(range(2))})
    status, report = repo.compare(grown, reduced)
    assert status == 0
    assert report["delta"][f"{measure}:{repo.root}/policy.py"] == -1
    assert report["delta"][f"{measure}Arms:{repo.root}/policy.py"] == -4


def test_dispatch_scopes_subjects_and_repeated_cases_do_not_combine(repo: Repository) -> None:
    before = repo.commit({})
    head = repo.commit({"scope.py": '''
def first(value):
    if value == "one": return 1
    if value == "two": return 2
    if other == "three": return 3
    if value == "two": return 2
    def inner(value):
        if value == "three": return 3
    return inner
async def second(value):
    if value == "three": return 3
'''} )
    status, report = repo.compare(before, head)
    assert status == 0
    assert report["head"][f"StringDispatch:{repo.root}/scope.py"] == 0
    assert report["head"][f"StringDispatchArms:{repo.root}/scope.py"] == 0


@pytest.mark.parametrize(("measure", "source"), [
    ("BooleanChainTerms", "valid = a and b and c and d\n"),
    ("ForeignAbsenceProbe", "absent = other.value is None\n"),
    ("CodecSubclass", "class Local(FieldCodec):\n    pass\n"),
    ("StringDispatch", 'def decide(x):\n    return x in ("a", "b", "c")\n'),
    ("TypeSwitch", 'def decide(x):\n    return isinstance(x, A) or isinstance(x, B) or isinstance(x, C)\n'),
])
def test_per_file_increase_cannot_be_offset_or_moved(repo: Repository, measure: str, source: str) -> None:
    base = repo.commit({"old.py": source, "new.py": ""})
    head = repo.commit({"old.py": None, "new.py": source})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"{measure}:{repo.root}/new.py"] > 0
    assert sum(delta for key, delta in report["delta"].items() if key.startswith(measure + ":")) == 0


def test_foreign_probes_and_codec_bases_use_ast_not_text(repo: Repository) -> None:
    base = repo.commit({})
    head = repo.commit({"boundary.py": """
class Owned:
    def inspect(self, other):
        local = self.value is None
        local_truth = not self.value
        foreign = other.value is not None
        foreign_truth = not other.value
        nested = self.other.value is None
        text = 'not other.value'
class LocalCodec(package.FieldCodec):
    pass
class OwnedCodec:
    pass
"""})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["head"][f"ForeignAbsenceProbe:{repo.root}/boundary.py"] == 3
    assert report["head"][f"CodecSubclass:{repo.root}/boundary.py"] == 1


def test_type_expression_boundary_keeps_runtime_dictionary_access(repo: Repository) -> None:
    source = '\n'.join([
        'import typing as t',
        'from typing import Literal as Choice',
        'transport: Choice["stdio"]',
        'class Declaration:',
        '    transport: t.Literal["stdio"]',
        'def render(value: Choice["stdio"]) -> t.Literal["stdio"]:',
        '    local: Choice["stdio"]',
        '    return "stdio"',
        'async def asynchronous(value: Choice["stdio"]) -> Choice["stdio"]:',
        '    return "stdio"',
    ]) + '\n'
    if hasattr(ast, "TypeAlias"):
        source += 'type Transport = t.Literal["stdio"]\n'
        source += 'def generic[T: t.Literal["stdio"]](value: T) -> T:\n    return value\n'
    base = repo.commit({"boundary.py": ""})
    declarations = repo.commit({"boundary.py": source})
    status, report = repo.compare(base, declarations)
    assert status == 0
    assert report["head"]["StringSubscript"] == 0
    # Annotation spelling cannot conceal actual initializer/default/decorator/body reads.
    runtime = source + '\n'.join([
        'transport: Choice["stdio"] = payload["transport"]',
        'dynamic_type: schema["type"]',
        '@decorators["render"]',
        'def read(value: Choice["stdio"] = defaults["transport"]) -> Choice["stdio"]:',
        '    return payload["transport"]',
    ]) + '\n'
    changed = repo.commit({"boundary.py": runtime})
    status, report = repo.compare(declarations, changed)
    assert status == 1
    assert report["delta"]["StringSubscript"] == 5


def test_typing_spelling_does_not_hide_shadowed_or_unresolved_read(repo: Repository) -> None:
    base = repo.commit({"shadow.py": ""})
    head = repo.commit({"shadow.py": '\n'.join([
        'from typing import Literal',
        'def read(Literal):',
        '    return Literal["value"]',
        'unknown: schema["type"]',
    ]) + '\n'})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"]["StringSubscript"] == 2
