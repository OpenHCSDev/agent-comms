"""Exercise the command against real Git commits, without mocking Git or ASTs."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.debt_ratchet import (
    BannedVocabulary, BroadExceptDefault, BroadSwallow, ForeignFieldSpellings,
    FrozenFieldWrite, FunctionLocalImport, GitMeasure, NoneAbsence, UncarriedRecordFormat,
    ComparedKind, Comparison, DeclaredSpellings, NamedAccess, NamedDeclaration,
    SourceLines, StringIdentity, python_paths,
)

pytestmark = pytest.mark.refactor_guard


@pytest.mark.parametrize("name,blocked", [
    (SourceLines.__name__, False),
    (f"{NoneAbsence.__name__}:tools/example.py", True),
])
def test_measure_owner_decides_whether_growth_blocks(name: str, blocked: bool) -> None:
    result = Comparison("tools", "base", "head", (), {name: 0}, {name: 1}, {}, {})
    assert result.delta[name] == 1
    assert result.failed is blocked


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
            [sys.executable, str(Path(__file__).resolve().parents[2] / "tools/debt_ratchet.py"),
             "--root", self.root, "--base", base, "--head", head],
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=120,  # Each comparison type-checks both revisions.
            check=False,
        )
        assert result.returncode in (0, 1), result.stderr[-2000:]
        return result.returncode, json.loads(result.stdout)


@pytest.fixture(params=["src/agent_comms", "src/toad"])
def repo(tmp_path: Path, request) -> Repository:
    return Repository(tmp_path, request.param)


def test_batch_sources_keep_exact_git_bytes_and_path_names(repo: Repository, monkeypatch) -> None:
    files = {
        'ignored.py': 'value = 1\n',
        'substituted.py': "stamp = '$Format:%H$'\n",
        'a b.py': 'value = 2\n',
        '[literal].py': 'value = 3\n',
        'line\nbreak.py': 'value = 4\n',
        'unicode-π.py': 'value = 5\n',
        'duplicate.py': 'value = 1\n',
        '.gitattributes': 'ignored.py export-ignore\nsubstituted.py export-subst\n',
    }
    revision = repo.commit(files)
    selected = {f'{repo.root}/{name}' for name in files if name.endswith('.py')}
    assert python_paths(repo.path, revision, repo.root) == selected
    expected = [(path, subprocess.check_output(
        ['git', '-C', str(repo.path), 'show', f'{revision}:{path}']
    )) for path in sorted(selected)]
    trace = repo.path / 'git-trace.json'
    monkeypatch.setenv('GIT_TRACE2_EVENT', str(trace))
    assert GitMeasure.sources(repo.path, revision, selected) == expected
    starts = [json.loads(line) for line in trace.read_text().splitlines()
              if json.loads(line)['event'] == 'start']
    assert len(starts) == 2
    assert GitMeasure.sources(repo.path, revision, set()) == []
    with pytest.raises(ValueError, match='omitted'):
        GitMeasure.sources(repo.path, revision, selected | {f'{repo.root}/missing.py'})


def test_format_probe_keeps_parent_dependencies_and_reads_archived_source(tmp_path, monkeypatch):
    repo = Repository(tmp_path, "src/agent_comms")
    source = subprocess.check_output(
        ["git", "archive", "HEAD", "--", "src/agent_comms"], timeout=20
    )
    subprocess.run(["tar", "-x", "-C", str(tmp_path)], input=source, check=True, timeout=60)
    dependency = tmp_path / "dependencies"
    dependency.mkdir()
    (dependency / "parent_only_dependency.py").write_text("VERSION = 7\n")
    inherited = os.pathsep.join((str(dependency), os.environ["PYTHONPATH"]))
    monkeypatch.setenv("PYTHONPATH", inherited)
    revision = repo.commit({"probe_record.py": (
        "from dataclasses import dataclass\nfrom typing import ClassVar\n"
        "from parent_only_dependency import VERSION\n"
        "@dataclass(frozen=True)\nclass Record:\n"
        "    version: ClassVar[int] = VERSION\n    value: str\n"
    )})
    (tmp_path / "src/agent_comms/probe_record.py").write_text("raise RuntimeError('not archived')\n")
    forms = UncarriedRecordFormat.formats(
        tmp_path, revision, repo.root, {"src/agent_comms/probe_record.py"}
    )
    assert forms["agent_comms.probe_record.Record"].version == 7
    assert os.environ["PYTHONPATH"] == inherited


def test_clean_growth_passes_but_type_and_string_dispatch_fail(repo: Repository) -> None:
    base = repo.commit({"new.py": "value = 1\n"})
    source = "value = 1\nother = 2\n"
    grown = repo.commit({"new.py": source})
    assert repo.compare(base, grown)[0] == 0
    filtered = repo.commit({"new.py": source + "filtered = type(value) is int\n"})
    status, report = repo.compare(grown, filtered)
    assert status == 1
    assert report["delta"]["TypeIdentity"] == 1
    dispatch = repo.commit({"new.py": source + (
        "\n\ndef select(value):\n"
        '    if value == "one":\n        return 1\n'
        '    if value == "two":\n        return 2\n'
        '    if value == "three":\n        return 3\n'
        "    return 0\n"
    )})
    status, report = repo.compare(grown, dispatch)
    assert status == 1
    assert report["delta"][f"StringDispatch:{repo.root}/new.py"] == 1


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
    source = 'x = row = {"key": 1}\nvalue = type(x) is not int\nread = row["key"]\n'
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


def test_builtin_handler_measure_uses_shared_collector_and_rejects_growth(repo: Repository) -> None:
    from tools.debt_ratchet import BuiltinHandlerTypeSwitch
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
    assert BuiltinHandlerTypeSwitch.count(source.encode(), "policy.py") == 1
    assert BuiltinHandlerTypeSwitch.count(growing.encode(), "policy.py") == 3
    assert BuiltinHandlerTypeSwitch.count(b"not valid Python", "field_codec.py") == 0
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


@pytest.mark.parametrize("root", ["src/agent_comms", "src/toad", "src/textual"])
def test_family_flattening_cannot_offset_another_file_or_admit_a_named_facade(tmp_path: Path, root: str):
    repo = Repository(tmp_path, root)
    from tools.debt_ratchet import FamilyFlattened
    from refactor_audit.measures import FamilyFlattened as AuditFamilyFlattened

    original = "def carry(value):\n    return (value.declared_name, value.family_name)\n"
    base = repo.commit({"first.py": original, "second.py": "value = 1\n"})
    head = repo.commit({"first.py": "def carry(value):\n    return value\n",
                        "second.py": "def carry(value):\n    return value.declared_name\n"})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"FamilyFlattened:{repo.root}/first.py"] == -2
    assert report["delta"][f"FamilyFlattened:{repo.root}/second.py"] == 1
    assert FamilyFlattened.count(original.encode(), "consumer.py") == (
        AuditFamilyFlattened.count(ast.parse(original))
    )
    original_modules = tuple(module for module in FamilyFlattened.mechanism_modules
                             if module.parent == Path(repo.root))
    facades = {f"{directory}/{module.parent.name}/{module.name}"
               for module in FamilyFlattened.mechanism_modules
               for directory in ("nested", "vendor")}
    facades.update(f"nested/{module.name}" for module in FamilyFlattened.mechanism_modules)
    moved = repo.commit({"second.py": "def carry(value):\n    return value\n",
                         "another_codec.py": original,
                         **{module.name: original for module in original_modules},
                         **{path: original for path in facades}})
    status, report = repo.compare(head, moved)
    assert status == 1
    assert report["delta"][f"FamilyFlattened:{repo.root}/another_codec.py"] == 2
    for module in original_modules:
        assert report["head"][f"FamilyFlattened:{module.as_posix()}"] == 0
    for path in facades:
        assert report["head"][f"FamilyFlattened:{repo.root}/{path}"] == 2



@pytest.mark.parametrize(("source", "expected"), [
    ("class Owner:\n def name(self):\n  return self.declared_name\n", 0),
    ("class Owner:\n @classmethod\n def name(cls):\n  return cls.declared_name\n", 0),
    ("class Owner:\n @classmethod\n def name(cls):\n  return Owner.declared_name\n", 0),
    ("class Owner:\n def name(self):\n  return Other.declared_name\n", 1),
    ("def name(value):\n return value.declared_name\n", 1),
    ("def name(value):\n return FieldCodec.decode(str, FieldCodec.encode(value))\n", 1),
    ("class Owner:\n @classmethod\n def name(cls):\n  return FieldCodec.decode(str, FieldCodec.encode(cls))\n", 1),
    ("def name(value):\n return FieldCodec.decode(str, FieldCodec.encode(value.declared_name))\n", 1),
    ("def name(value):\n return FieldCodec.decode(str, value)\n", 0),
    ("class Owner:\n def name(self, other):\n  return (self.declared_name, other.declared_name)\n", 1),
    ("if __name__ == '__main__':\n    main()\n", 0),
    ("def name(value):\n return value.name == 'released'\n", 1),
])
def test_family_name_owner_reads_and_codec_detours(source: str, expected: int):
    from tools.debt_ratchet import FamilyFlattened

    assert FamilyFlattened.count(source.encode(), "consumer.py") == expected


def test_small_owners_stay_within_the_class_threshold(repo: Repository) -> None:
    base = repo.commit({"owners.py": owner("Small", 2)})
    head = repo.commit({"owners.py": owner("Small", 500) + owner("New", 500)})
    status, report = repo.compare(base, head)
    assert status == 0
    assert report["delta"][f"GodClassExcess:{repo.root}/owners.py::Small"] == 0
    assert report["delta"][f"GodClassExcess:{repo.root}/owners.py::New"] == 0


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


def test_class_move_keeps_baseline_and_new_small_owner_has_no_class_debt(repo: Repository) -> None:
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
    # This checks the three-arm dispatch measure's scope. The separate
    # family measure also reports these literal comparisons.
    assert status == 1
    assert report["head"][f"FamilyFlattened:{repo.root}/scope.py"] > 0
    assert report["head"][f"StringDispatch:{repo.root}/scope.py"] == 0
    assert report["head"][f"StringDispatchArms:{repo.root}/scope.py"] == 0


@pytest.mark.parametrize(("measure", "source"), [
    ("BooleanChainTerms", "valid = a and b and c and d\n"),
    ("ForeignAbsenceProbe", "absent = other.value is None\n"),
    ("CodecSubclass", "class Local(FieldCodec):\n    pass\n"),
    ("StringDispatch", 'def decide(x):\n    return x in ("a", "b", "c")\n'),
    ("TypeSwitch", 'def decide(x):\n    return isinstance(x, A) or isinstance(x, B) or isinstance(x, C)\n'),
    ("AnyAnnotation", "from typing import Any\ndef read(value: Any) -> None: ...\n"),
    ("Unformatted", "value=1\n"),
    ("LintFindings", "import os\n"),
    ("TypeErrors", 'value: int = "text"\n'),
])
def test_per_file_increase_cannot_be_offset_or_moved(repo: Repository, measure: str, source: str) -> None:
    base = repo.commit({"old.py": source, "new.py": ""})
    head = repo.commit({"old.py": None, "new.py": source})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"{measure}:{repo.root}/new.py"] > 0
    assert sum(delta for key, delta in report["delta"].items() if key.startswith(measure + ":")) == 0


def test_both_revisions_are_checked_under_the_head_configuration(repo: Repository) -> None:
    source = "import sys\nimport os\n\nvalue: int = 'text'\nused = (os, sys)\n"
    (repo.path / "pyproject.toml").write_text(
        '[tool.ruff.lint]\nselect = ["E", "F"]\n[tool.mypy]\npython_version = "3.11"\n')
    base = repo.commit({"kept.py": source + "type Pair = tuple[int, int]\n"})
    (repo.path / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["E", "F", "I"]\n')
    head = repo.commit({"kept.py": source + "type Pair = tuple[int, int]\nother = 1\n"})
    status, report = repo.compare(base, head)
    # The base alone cannot parse PEP 695 under its own 3.11 target, and the head adds the
    # import-order rule; under one configuration neither changes the file's counts.
    assert report["base"][f"TypeErrors:{repo.root}/kept.py"] == 1
    assert report["base"][f"LintFindings:{repo.root}/kept.py"] == 1
    for measure in ("TypeErrors", "LintFindings", "Unformatted"):
        assert report["delta"][f"{measure}:{repo.root}/kept.py"] == 0


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
    assert report["head"][f"ForeignAbsenceProbe:{repo.root}/boundary.py"] == 2
    assert report["head"][f"CodecSubclass:{repo.root}/boundary.py"] == 1


def test_connection_boolean_is_not_an_absence_probe(repo: Repository) -> None:
    base = repo.commit({})
    boolean = repo.commit({"connection.py": """
from multiprocessing.connection import Connection

class Reader:
    connection: Connection

    def readable(self):
        return not self.connection.closed
"""})
    status, report = repo.compare(base, boolean)
    assert status == 1
    assert report["head"][f"ForeignAbsenceProbe:{repo.root}/connection.py"] == 0

    missing = repo.commit({"missing.py": """
class State:
    value: str | None

def missing(state: State):
    return state.value is None

def present(state: State):
    return state.value is not None
"""})
    status, report = repo.compare(boolean, missing)
    assert status == 1
    assert report["delta"][f"ForeignAbsenceProbe:{repo.root}/missing.py"] == 2


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
    assert status == 1
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


ANY_DECLARATIONS = '\n'.join([
    'import typing as t',
    'from dataclasses import dataclass',
    'from typing import TYPE_CHECKING, Annotated, Callable, Literal, Optional, TypeAlias',
    'if TYPE_CHECKING:',
    '    from typing import Any',
    'Payload = dict[str, Any]',
    'Row: TypeAlias = list[Any]',
    '@dataclass(frozen=True)',
    'class Record:',
    '    fill: Any = 0',
    '    quoted: "Optional[Any]" = None',
    'class Plain:',
    '    cache: t.Any',
    '    def read(self, value: Any | None, *rest: Any, **named: Any) -> Callable[..., Any]:',
    '        local: Annotated[Any, "Any"] = value',
    '        return local',
]) + '\n'

# Each line names a type or a value without declaring ``Any`` as a type.
ANY_NON_DECLARATIONS = '\n'.join([
    'from typing import Annotated, Any, Literal',
    'choice: Literal["Any"]',
    'described: Annotated[int, Any]',
    'skipped = (Any, object)',
    'chosen = handlers["Any"]',
    'def unstated(annotation: object) -> bool:',
    '    return annotation is Any or isinstance(annotation, str)',
    'class Local:',
    '    Any = int',
]) + '\n'


def test_any_is_counted_wherever_a_type_is_declared(repo: Repository) -> None:
    from tools.debt_ratchet import AnyAnnotation

    uses = AnyAnnotation.uses(ANY_DECLARATIONS.encode(), "declarations.py")
    assert sorted((use.site, use.owner) for use in uses) == [
        ("alias", ""), ("alias", ""),
        ("class attribute", "Plain"),
        ("dataclass field", "Record"), ("dataclass field", "Record"),
        ("parameter", "Plain.read"), ("parameter", "Plain.read"), ("parameter", "Plain.read"),
        ("return", "Plain.read"),
        ("variable", "Plain.read"),
    ]
    assert AnyAnnotation.count(ANY_NON_DECLARATIONS.encode(), "values.py") == 0

    base = repo.commit({"declarations.py": "", "values.py": ""})
    head = repo.commit({"declarations.py": ANY_DECLARATIONS, "values.py": ANY_NON_DECLARATIONS})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"AnyAnnotation:{repo.root}/declarations.py"] == len(uses)
    assert report["delta"][f"AnyAnnotation:{repo.root}/values.py"] == 0


def test_stating_a_type_lowers_the_any_count_and_passes(repo: Repository) -> None:
    base = repo.commit({"declarations.py": ANY_DECLARATIONS})
    head = repo.commit({"declarations.py": ANY_DECLARATIONS.replace("fill: Any = 0", "fill: int = 0")})
    status, report = repo.compare(base, head)
    assert status == 0
    assert report["delta"][f"AnyAnnotation:{repo.root}/declarations.py"] == -1


@pytest.mark.parametrize(("measure", "source"), [
    (FrozenFieldWrite, 'object.__setattr__(self, "value", 1)\n'),
    (BannedVocabulary, 'pending_receipt = 1\n'),
])
def test_clean_changed_lines_cannot_be_offset_with_a_deletion(repo, measure, source):
    base = repo.commit({"old.py": source, "new.py": ""})
    head = repo.commit({"old.py": None, "new.py": source})
    status, report = repo.compare(base, head)
    identity = f"{measure.__name__}:{repo.root}/new.py"
    assert status == 1
    assert report["delta"][identity] == 1
    assert report["introduced"][identity] == 1


@pytest.mark.parametrize(("measure", "source"), [
    (FrozenFieldWrite, 'object.__setattr__(self, "value", 1)\n'),
    (BannedVocabulary, 'pending_receipt = 1\n'),
])
def test_unchanged_old_sites_are_not_forced_to_zero(repo, measure, source):
    base = repo.commit({"old.py": source + "clean = 1\n"})
    head = repo.commit({"old.py": source + "clean = 2\n"})
    status, report = repo.compare(base, head)
    identity = f"{measure.__name__}:{repo.root}/old.py"
    assert status == 0
    assert report["head"][identity] == 1
    assert report["introduced"][identity] == 0


def test_none_as_absence_fails_with_its_line_except_in_a_declared_boundary_decoder(repo):
    marked = ("from agent_comms.field_codec import boundary_decoder\n\n\n"
              "@boundary_decoder\ndef decoded(value: str):\n    return value\n")
    files = {"plan.py": marked, "foreign.py": marked.replace("agent_comms.field_codec", "elsewhere"),
             "owner.py": "def owner(value: str):\n    return value\n"}
    base = repo.commit(files)
    head = repo.commit({name: source.replace("value: str", "value: str | None")
                        for name, source in files.items()})
    status, report = repo.compare(base, head)
    identity = f"{NoneAbsence.__name__}:{repo.root}"
    assert status == 1
    assert report["introduced_lines"] == {f"{identity}/owner.py": [1], f"{identity}/foreign.py": [5]}
    assert report["introduced"][f"{identity}/plan.py"] == report["delta"][f"{identity}/plan.py"] == 0


def test_git_context_configuration_cannot_mark_unchanged_lines_as_new(repo):
    repo.git("config", "diff.interHunkContext", "3")
    base = repo.commit({"names.py": "first = 1\nold_receipt = 1\nlast = 1\n"})
    head = repo.commit({"names.py": "first = 2\nold_receipt = 1\nlast = 2\n"})
    status, report = repo.compare(base, head)
    identity = f"BannedVocabulary:{repo.root}/names.py"
    assert status == 0
    assert report["head"][identity] == 1
    assert report["introduced"][identity] == 0


@pytest.mark.parametrize("word", BannedVocabulary.words)
def test_net_reduction_does_not_pay_for_a_new_identifier(repo, word):
    base = repo.commit({"names.py": "old_admit = 1\nold_receipt = 2\n"})
    head = repo.commit({"names.py": f"new_{word} = 3\n"})
    status, report = repo.compare(base, head)
    identity = f"BannedVocabulary:{repo.root}/names.py"
    assert status == 1
    assert report["base"][identity] == 2
    assert report["head"][identity] == 1
    assert report["introduced"][identity] == 1


@pytest.mark.parametrize("word", BannedVocabulary.words)
def test_old_function_name_and_multiline_attribute_have_their_own_lines(repo, word):
    base = repo.commit({"names.py": f"def old_admit():\n    return (\n        peer\n        .{word}\n    )\n"})
    head = repo.commit({"names.py": f"def old_admit():\n    return (\n        other\n        .{word}\n    )\n"})
    status, report = repo.compare(base, head)
    identity = f"BannedVocabulary:{repo.root}/names.py"
    assert status == 0
    assert report["head"][identity] == 2
    assert report["introduced"][identity] == 0


def test_changed_argument_still_changes_a_multiline_frozen_write(repo):
    base = repo.commit({"owner.py": 'object.__setattr__(\n    self, "value",\n    1,\n)\n'})
    head = repo.commit({"owner.py": 'object.__setattr__(\n    self, "value",\n    2,\n)\n'})
    status, report = repo.compare(base, head)
    identity = f"FrozenFieldWrite:{repo.root}/owner.py"
    assert status == 1
    assert report["delta"][identity] == 0
    assert report["introduced"][identity] == 1


@pytest.mark.parametrize("word", BannedVocabulary.words)
def test_vocabulary_includes_identifiers_comments_and_docstring_lines_only(word):
    source = f'''"""Receipt and {word} share one prose line.\nAdmitted work.\n"""\n# admission\nkey = "admission_generation"\ndef admit_receipt({word.title()}, **clean):\n    return peer.receipt({word.title()}, admission=1)\n'''.encode()
    # Three prose lines; function, parameter, attribute, loaded parameter and keyword.
    assert BannedVocabulary.count(source, "values.py") == 8
    assert FrozenFieldWrite.count(b'text = "object.__setattr__(x, y, z)"\n', "values.py") == 0


def test_changed_prose_fails_but_an_unchanged_disk_key_does_not(repo):
    original = 'key = "admission_generation"\n# clean wording\n'
    base = repo.commit({"words.py": original})
    head = repo.commit({"words.py": original.replace("clean wording", "admission wording")})
    status, report = repo.compare(base, head)
    identity = f"BannedVocabulary:{repo.root}/words.py"
    assert status == 1
    assert report["head"][identity] == 1
    assert report["introduced"][identity] == 1


def test_local_import_scope_counts_nested_functions_and_classes_once():
    source = b'''import top\nclass ModuleClass:\n    import top_class\ndef outer():\n    import one\n    class Local:\n        import two\n        def method(self):\n            from package import three\n    async def inner():\n        import four\n'''
    assert FunctionLocalImport.count(source, "imports.py") == 4


def string_identity_readings(source: bytes) -> list[tuple[str, type]]:
    declared = b"class CarryStep: pass\ndef carry_hunts(): pass\n"
    spellings = DeclaredSpellings.read([("tools/carry_hunts.py", declared), ("src/pkg/__init__.py", b""),
                                         ("src/pkg/route_table.py", b""), ("tools/use.py", source)])
    return [(site.spelling, site.reading) for site in spellings.sites(source, "tools/use.py")]


def test_string_identity_reads_declared_names_compared_kinds_and_named_access():
    source = b'''after = "carry_hunts"
step = "CarryStep"
table = "pkg.route_table"
if kind == "release": pass
if mode in {"fast", "slow"}: pass
match shape:
    case "square": pass
getattr(peer, "model")
getattr(peer, name)
importlib.import_module("pkg.plugin")
Family.member_named("text")
text = "getattr(peer, 'model')"
word = "plain words here"
'''
    assert string_identity_readings(source) == [
        ("carry_hunts", NamedDeclaration), ("CarryStep", NamedDeclaration),
        ("pkg.route_table", NamedDeclaration), ("release", ComparedKind), ("fast", ComparedKind),
        ("slow", ComparedKind), ("square", ComparedKind), ("model", NamedAccess),
        ("pkg.plugin", NamedAccess), ("text", NamedAccess),
    ]


def test_string_identity_exempts_prose_interpolation_enum_bodies_class_options_and_entry_guard():
    source = b'''"""carry_hunts is described here."""
import enum
class Wire(enum.StrEnum):
    STEP = "carry_hunts"
class Hook(Wire):
    OTHER = "CarryStep"
class Member(Base, declared_name="carry_hunts"):
    """CarryStep docs."""
label = f"{name} carry_hunts"
if __name__ == "__main__": pass
'''
    assert string_identity_readings(source) == []


def test_string_identity_measures_spellings_outside_root_and_skips_declared_tests(repo: Repository) -> None:
    (repo.path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    outside = {"tools/carry_other.py": "def run(): pass\n", "tools/step.py": "after = None\n",
               "tests/test_step.py": "after = None\n"}
    for name, source in outside.items():
        (repo.path / name).parent.mkdir(parents=True, exist_ok=True)
        (repo.path / name).write_text(source)
    base = repo.commit({"kept.py": "value = 1\n"})
    for name in ("tools/step.py", "tests/test_step.py"):
        (repo.path / name).write_text('after = "carry_other"\n')
    head = repo.commit({})
    status, report = repo.compare(base, head)
    assert status == 1
    assert {key: value for key, value in report["delta"].items() if value} == {"StringIdentity:tools/step.py": 1}
    assert "StringIdentity:tests/test_step.py" not in report["head"]


def test_broad_suppress_is_counted_and_cannot_be_added(repo):
    source = b'''with suppress(OSError): work()
with contextlib.suppress(ConnectionError, TimeoutError): work()
with suppress(FileNotFoundError): absent()
with suppress(Exception): raise RuntimeError()
'''
    assert BroadSwallow.count(source, "read.py") == 3
    before = repo.commit({"reader.py": "work()\n"})
    after = repo.commit({"reader.py": "with suppress(OSError): work()\n"})
    status, report = repo.compare(before, after)
    identity = f"BroadSwallow:{repo.root}/reader.py"
    assert status == 1 and report["introduced"][identity] == 1
    assert report["delta"][identity] == 1


def test_field_spelling_rosters_count_loops_not_declarations():
    source = b'''fields = ("path", "file_path", "filePath")\nfor key in ("path", "file_path", "filePath"):\n    value = data[key]\nvalue = next(data[key] for key in ("path", "file_path", "filePath") if key in data)\n'''
    assert ForeignFieldSpellings.count(source, "read.py") == 2


def test_broad_swallow_is_counted_and_cannot_be_added(repo):
    source = b'''try: work()
except (OSError, ValueError): pass
try: work()
except Exception: return_default()
try: work()
except: continue_work()
try: work()
except RuntimeError: raise
try: work()
except FileNotFoundError: absent()
'''
    assert BroadSwallow.count(source, "read.py") == 3
    root = Path(__file__).resolve().parents[2] / "src/agent_comms"
    assert sum(BroadSwallow.count(path.read_bytes(), str(path))
               for path in root.rglob("*.py")) <= 29
    before = repo.commit({"reader.py": "work()\n"})
    after = repo.commit({"reader.py": "try: work()\nexcept OSError: pass\n"})
    status, report = repo.compare(before, after)
    identity = f"BroadSwallow:{repo.root}/reader.py"
    assert status == 1 and report["introduced"][identity] == 1
    assert report["delta"][identity] == 1


def test_broad_handlers_only_count_defaults_returned_by_the_handler():
    source = b'''def read():\n    try: work()\n    except Exception:\n        return None\n    try: work()\n    except:\n        if condition: return {}\n    try: work()\n    except OSError:\n        return None\n    try: work()\n    except BaseException:\n        raise\n    try: work()\n    except Exception:\n        def helper(): return None\n        raise\n'''
    assert BroadExceptDefault.count(source, "read.py") == 2
