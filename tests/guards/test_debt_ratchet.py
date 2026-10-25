"""Exercise the command against real Git commits, without mocking Git or ASTs."""

import ast
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_comms.child_process import ObservedProcess, SpawnedProcess
from agent_comms.field_codec import FieldCodec
from fixture_git import IDENTITY
from process_tree_fixture import TREE, identities, ready

from debt_ratchet import (
    AUDIT_SCRIPTS,
    BannedVocabulary,
    BroadExceptDefault,
    BroadSwallow,
    ContentCache,
    DonePhaseBroken,
    ForeignFieldSpellings,
    FrozenFieldWrite,
    EnumDeclaration,
    FunctionLocalImport,
    GitMeasure,
    NoneAbsence,
    UncarriedRecordFormat,
    ComparedKind,
    Comparison,
    DeclaredSpellings,
    NamedAccess,
    NamedDeclaration,
    PlainStrIdAnnotation,
    StringFieldMetadata,
    StringIdentity,
    python_paths,
    RawRecordShapes,
    AnyName,
    BooleanChainTerms,
    BuiltinHandlerTypeSwitch,
    CodecSubclass,
    FamilyFlattened,
    ForeignAbsenceProbe,
    GodClassExcess,
    LintFindings,
    LongBooleanChain,
    Measure,
    OwnTypeTest,
    StringDispatch,
    StringDispatchArms,
    StoreLockCalls,
    StringSubscript,
    TypeErrors,
    TypeIdentity,
    TypeSwitch,
    TypeSwitchArms,
    UnannotatedParameter,
    Unformatted,
    compare,
)

pytestmark = pytest.mark.refactor_guard


def test_measure_owner_rejects_a_new_absence() -> None:
    name = NoneAbsence.at("tools/example.py")
    result = Comparison("tools", "base", "head", (), {name: 0}, {name: 1}, {}, {})
    assert result.delta[name] == 1
    assert result.failed


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from dataclasses import field as f\nx=f(metadata={'sql': 1, 'wire_name': 'x'})", 2),
        ("import dataclasses as dc\nx=dc.field(metadata={'sql': 1})", 1),
        ("from dataclasses import field\nx=field(metadata=options)", 1),
        ("from dataclasses import field\nx=field(metadata={**options})", 1),
        ("from dataclasses import field\nx=field(default=None)", 0),
        ("from another_library import field\nx=field(metadata={'ui_hidden': True})", 0),
    ],
)
def test_string_field_metadata_resolves_imports_and_refuses_opaque_keys(source, expected):
    assert StringFieldMetadata.count(source.encode(), "example.py") == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("turn_id: str", 1),
        ("turn_id: TurnId", 0),
        ("session_id: str", 1),
        ("goal_id: str | None", 1),
        ("goal_id: None | str", 1),
        ("goal_id: Annotated[str, Id128Text]", 1),
        ("goal_id: typing.Annotated[str, Id128Text] | None", 1),
        ("goal_id: Annotated[GoalId, Metadata]", 0),
        ("def f(turn_id: str, session_id: str | None): pass", 2),
        ("def f(*, lookup: str, nonce: str, generation: str): pass", 3),
        ("self.turn_id: str", 1),
        ("description: str", 0),
        ("turn_id: bytes", 0),
    ],
)
def test_plain_str_id_annotations_block_growth_per_file(source, expected):
    path = "src/example.py"
    before = PlainStrIdAnnotation.snapshot([(path, b"description: str")])
    after = PlainStrIdAnnotation.snapshot([(path, source.encode())])
    identity = PlainStrIdAnnotation.at(path)
    comparison = Comparison("src", "base", "head", (path,), before, after, {}, {})
    assert comparison.delta[identity] == expected
    assert comparison.failed == bool(expected)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("with store.locked(mode=SharedLock()):\n    pass", 1),
        ("async with store.locked_async():\n    pass", 1),
        ("with _store_lock(path), StoreLock.ordered(owners):\n    pass", 2),
        ("fcntl.flock(descriptor, fcntl.LOCK_EX)", 1),
        ("def try_store_lock(descriptor):\n    is_store_lock(path)", 0),
        ('"""with store.locked():"""\nnote = "fcntl.flock("', 0),
        ("ordered(owners)\nlocked = True", 0),
    ],
)
def test_store_lock_calls_count_calls_not_spellings(source, expected):
    assert StoreLockCalls.count(source.encode(), "src/agent_comms/example.py") == expected


def test_store_lock_calls_skip_the_store_owners():
    """Locking is store_files' and locked_store's own mechanism, never a hand-placed copy."""
    source = b"with _store_lock(path):\n    pass"
    assert StoreLockCalls.owners == ("agent_comms/store_files.py", "agent_comms/locked_store.py")
    assert [StoreLockCalls.count(source, f"src/{owner}") for owner in StoreLockCalls.owners] == [0, 0]


def test_a_new_store_lock_call_fails_on_its_own_file_with_its_line(repo):
    base = repo.commit({"turns.py": "def save(store):\n    with store.locked():\n        pass\n",
                        "other.py": "def read(store):\n    with store.locked():\n        pass\n"})
    head = repo.commit({"turns.py": "def save(store):\n    pass\n",
                        "other.py": "def read(store):\n    with store.locked():\n        pass\n"
                                    "    with store.locked():\n        pass\n"})
    status, report = repo.compare(base, head, StoreLockCalls)
    assert status == 1
    grown = StoreLockCalls.at(f"{repo.root}/other.py").text
    assert report["delta"] == {StoreLockCalls.at(f"{repo.root}/turns.py").text: -1, grown: 1}
    assert report["found"] == {grown: [{"path": f"{repo.root}/other.py", "line": line}
                                       for line in (2, 4)]}


SOURCE = Path(__file__).resolve().parents[2]


def search_path(*first: Path) -> str:
    """first, then the inherited PYTHONPATH when the caller's environment has one."""
    inherited = os.environ.get("PYTHONPATH")
    return os.pathsep.join([*map(str, first), *([inherited] if inherited else [])])


def ratchet_environment() -> dict[str, str]:
    """The command's environment: this checkout's src before the inherited path."""
    return {**os.environ, "PYTHONPATH": search_path(SOURCE / "src")}


class Repository:
    def __init__(self, path: Path, root: str):
        self.path = path
        self.root = root
        self.git("init", "-q")
        self.git("remote", "add", "origin", str(SOURCE))

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", *IDENTITY, "-C", str(self.path), *args], text=True, timeout=10
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
        self.git("commit", "-qm", "test", "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def arguments(self, base: str, head: str) -> tuple[str, ...]:
        # pytest-xdist runs these comparisons side by side; each keeps a narrow pool.
        return ("--root", self.root, "--base", base, "--head", head, "--workers", "2")

    @contextmanager
    def comparing(self, base: str, head: str) -> Iterator[SpawnedProcess]:
        """The command launched as its own process, for the tests of its process tree."""
        command = (sys.executable, str(SOURCE / "tools/debt_ratchet.py"),
                   *self.arguments(base, head))
        with SpawnedProcess.launch(command, cwd=self.path, env=ratchet_environment(),
                                   output=subprocess.PIPE) as child:
            yield child

    def compare(self, base: str, head: str, *measured: type[Measure]) -> tuple[int, dict]:
        """The command's exit status and report, taking only the named measures' measurements.

        The command line itself is proven by the tests that launch it.
        """
        result = compare(self.path, base, head, self.root, 2, measured)
        return int(result.failed), json.loads(json.dumps(FieldCodec.project(result, "report")))


def test_compare_timeout_retires_pool_probe_and_defiant_grandchild(tmp_path: Path) -> None:
    repo = Repository(tmp_path, "src/sample")
    announcement = tmp_path / "probe-tree.json"
    declaration = (
        "from dataclasses import dataclass\nfrom typing import ClassVar\n"
        "@dataclass(frozen=True)\nclass Record:\n"
        "    version: ClassVar[int] = 1\n    value: str = ''\n"
    )
    # A real stored-format probe imports this committed module. Its child tree
    # announces exact identities before blocking; no production method is patched.
    source = declaration + f"\nimport sys\nsys.argv = ['tree', {str(announcement)!r}]\n" + TREE
    base = repo.commit({"__init__.py": "", "record.py": source})
    head = repo.commit({"record.py": source.replace("version: ClassVar[int] = 1",
                                                  "version: ClassVar[int] = 2")})
    observed = ()
    try:
        with repo.comparing(base, head) as child:
            asyncio.run(ready(announcement, child.alive))
            observed = identities(announcement)
            assert all(member.alive() for member in observed)
            pool = child.platform.group_members(child.identity)
            assert len(pool) > 1, "the real ratchet must have started its pool"
            with pytest.raises(subprocess.TimeoutExpired):
                child.collect(timeout=.05)
        after = child.platform.group_members(child.identity)
        surviving_probe = tuple(member for member in observed if member.alive())
        (tmp_path / "comparison-tree.json").write_text(json.dumps({
            "tool_group_before": [FieldCodec.encode(member) for member in pool],
            "probe_before": [FieldCodec.encode(member) for member in observed],
            "tool_group_after": [FieldCodec.encode(member) for member in after],
            "probe_after": [FieldCodec.encode(member) for member in surviving_probe],
        }, indent=2))
        assert not after
        assert not surviving_probe
    finally:
        # Failure cleanup has exact identities and runs after the assertions. It
        # cannot make the test pass by sweeping unrelated processes by name.
        for member in observed:
            if member.alive():
                ObservedProcess(member).stop_sync()


@pytest.fixture(params=["src/agent_comms", "src/toad"])
def repo(tmp_path: Path, request) -> Repository:
    return Repository(tmp_path, request.param)


def test_batch_sources_keep_exact_git_bytes_and_path_names(repo: Repository, monkeypatch) -> None:
    files = {
        "ignored.py": "value = 1\n",
        "substituted.py": "stamp = '$Format:%H$'\n",
        "a b.py": "value = 2\n",
        "[literal].py": "value = 3\n",
        "line\nbreak.py": "value = 4\n",
        "unicode-π.py": "value = 5\n",
        "duplicate.py": "value = 1\n",
        ".gitattributes": "ignored.py export-ignore\nsubstituted.py export-subst\n",
    }
    revision = repo.commit(files)
    selected = {f"{repo.root}/{name}" for name in files if name.endswith(".py")}
    assert python_paths(repo.path, revision, repo.root) == selected
    expected = [
        (path, subprocess.check_output(["git", "-C", str(repo.path), "show", f"{revision}:{path}"]))
        for path in sorted(selected)
    ]
    trace = repo.path / "git-trace.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    assert GitMeasure.sources(repo.path, revision, selected) == expected
    starts = [
        json.loads(line)
        for line in trace.read_text().splitlines()
        if json.loads(line)["event"] == "start"
    ]
    assert len(starts) == 2
    assert GitMeasure.sources(repo.path, revision, set()) == []
    with pytest.raises(ValueError):
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
    inherited = search_path(dependency)
    monkeypatch.setenv("PYTHONPATH", inherited)
    revision = repo.commit(
        {
            "probe_record.py": (
                "from dataclasses import dataclass\nfrom typing import ClassVar\n"
                "from parent_only_dependency import VERSION\n"
                "@dataclass(frozen=True)\nclass Record:\n"
                "    version: ClassVar[int] = VERSION\n    value: str\n"
            )
        }
    )
    (tmp_path / "src/agent_comms/probe_record.py").write_text(
        "raise RuntimeError('not archived')\n"
    )
    forms = UncarriedRecordFormat.formats(
        tmp_path, revision, repo.root, {"src/agent_comms/probe_record.py"}
    )
    assert forms["agent_comms.probe_record.Record"].version == 7
    assert os.environ["PYTHONPATH"] == inherited


def test_clean_growth_passes_but_type_and_string_dispatch_fail(repo: Repository) -> None:
    base = repo.commit({"new.py": "value = 1\n"})
    source = "value = 1\nother = 2\n"
    grown = repo.commit({"new.py": source})
    assert repo.compare(base, grown, StringDispatch, TypeIdentity)[0] == 0
    filtered = repo.commit({"new.py": source + "filtered = type(value) is int\n"})
    status, report = repo.compare(grown, filtered, StringDispatch, TypeIdentity)
    assert status == 1
    assert report["delta"]["TypeIdentity"] == 1
    dispatch = repo.commit(
        {
            "new.py": source
            + (
                "\n\ndef select(value):\n"
                '    if value == "one":\n        return 1\n'
                '    if value == "two":\n        return 2\n'
                '    if value == "three":\n        return 3\n'
                "    return 0\n"
            )
        }
    )
    status, report = repo.compare(grown, dispatch, StringDispatch, TypeIdentity)
    assert status == 1
    assert report["delta"][StringDispatch.at(f"{repo.root}/new.py").text] == 1


def test_added_type_check_fails(repo: Repository) -> None:
    base = repo.commit({"check.py": 'value = row["a"]\n'})
    head = repo.commit({"check.py": "value = type(x) is int\n"})
    status, report = repo.compare(base, head, LongBooleanChain, StringSubscript, TypeIdentity)
    assert status == 1  # A different measure's reduction cannot cancel this increase.
    assert {
        key: report["delta"][key] for key in ("TypeIdentity", "LongBooleanChain", "StringSubscript")
    } == {
        "TypeIdentity": 1,
        "LongBooleanChain": 0,
        "StringSubscript": -1,
    }


def test_a_done_phase_fails_the_change_that_breaks_it(tmp_path: Path) -> None:
    """OwnTypeChecksPhase holds at the base, so re-adding an isinstance on our own class
    fails at its line; the phases not done at the base (every merged-commit one, here)
    block nothing."""
    repo = Repository(tmp_path, "src")
    # The merged-commit phases name this repository's commits; borrowing its objects lets
    # them read "not an ancestor" of the fixture instead of fetching.
    objects = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "--path-format=absolute",
                                       "--git-common-dir"], text=True).strip()
    (tmp_path / ".git/objects/info/alternates").write_text(f"{objects}/objects\n")
    owned = "class Owned:\n    pass\n"
    base = repo.commit({"agent_comms/owned.py": owned})
    head = repo.commit({"agent_comms/owned.py": owned + "\n\ndef owns(value):\n"
                        "    return isinstance(value, Owned)\n"})
    status, report = repo.compare(base, head, DonePhaseBroken)
    assert status == 1
    assert report["delta"] == {
        "DonePhaseBroken:OwnTypeChecksPhase::src/agent_comms/owned.py:6": 1,
    }
    clean = repo.commit({"agent_comms/owned.py": owned + "\n\ndef owns(value):\n"
                         "    return value.owned()\n"})
    assert repo.compare(base, clean, DonePhaseBroken)[0] == 0


def test_moves_between_files_pass(repo: Repository) -> None:
    source = 'x = row = {"key": 1}\nvalue = type(x) is not int\nread = row["key"]\n'
    base = repo.commit({"from.py": source, "other.py": "a = 1\n"})
    moved = repo.commit({"from.py": None, "nested/to.py": source})
    combined = repo.commit({"nested/to.py": None, "other.py": "a = 1\n" + source})
    for head in (moved, combined):
        status, report = repo.compare(base, head, StringSubscript, TypeIdentity)
        assert status == 0
        assert not any(report["delta"].values())
        assert report["head"]["TypeIdentity"] == 1
        assert report["head"]["StringSubscript"] == 1


def test_reduction_passes(repo: Repository) -> None:
    base = repo.commit({"removed.py": 'value = type(x) is int and a and b and row["key"]\n'})
    head = repo.commit({"removed.py": None})
    status, report = repo.compare(base, head, LongBooleanChain, StringSubscript, TypeIdentity)
    assert status == 0
    assert {
        key: report["delta"][key] for key in ("TypeIdentity", "LongBooleanChain", "StringSubscript")
    } == {
        "TypeIdentity": -1,
        "LongBooleanChain": -1,
        "StringSubscript": -1,
    }


def test_new_file_smells_count(repo: Repository) -> None:
    base = repo.commit({})
    head = repo.commit(
        {
            "new.py": """
value = type(x) is int is not type(y)
other = type(z) == int
chain = a or b or c or d
short = a and b and c
read = row["key"]
row['other'] = read
numeric = row[0]
text = 'type(x) is int and a and b and row["not code"]'
# type(x) is int and a and b and row["not code"]
"""
        }
    )
    status, report = repo.compare(base, head, LongBooleanChain, StringSubscript, TypeIdentity)
    assert status == 1
    assert report["base"] == {name: 0 for name in report["head"]}
    assert {
        key: report["head"][key] for key in ("TypeIdentity", "LongBooleanChain", "StringSubscript")
    } == {
        "TypeIdentity": 2,
        "LongBooleanChain": 1,
        "StringSubscript": 2,
    }


def test_other_source_root_does_not_enter_report(repo: Repository) -> None:
    base = repo.commit({"kept.py": "value = 1\n"})
    outside = repo.path / "unrelated.py"
    outside.write_text('value = type(x) is int and a and b and row["key"]\n')
    head = repo.commit({"kept.py": "value = 2\n"})
    status, report = repo.compare(base, head, TypeIdentity, LongBooleanChain, StringSubscript)
    assert status == 0
    assert report["paths"] == [f"{repo.root}/kept.py"]


def test_builtin_handler_measure_uses_shared_collector_and_rejects_growth(repo: Repository) -> None:
    from audit.handler_declarations import BuiltinHandlerDeclarations

    assert issubclass(BuiltinHandlerTypeSwitch, BuiltinHandlerDeclarations)
    source = """from .mro_dispatch import MroDispatch, handles as cases
class Consumer(MroDispatch):
    @cases(dict)
    def one(self, value): pass
    @cases(DomainRecord)
    def domain(self, value): pass
"""
    base = repo.commit({"policy.py": source})
    growing = source.replace("@cases(DomainRecord)", "@cases(list, str)")
    assert BuiltinHandlerTypeSwitch.count(source.encode(), "policy.py") == 1
    assert BuiltinHandlerTypeSwitch.count(growing.encode(), "policy.py") == 3
    assert BuiltinHandlerTypeSwitch.count(b"not valid Python", "field_codec.py") == 0
    head = repo.commit({"policy.py": growing})
    status, report = repo.compare(base, head, BuiltinHandlerTypeSwitch)
    identity = BuiltinHandlerTypeSwitch.at(f"{repo.root}/policy.py").text
    assert status == 1
    assert report["base"][identity] == 1 and report["head"][identity] == 3
    assert report["delta"][identity] == 2
    moved = repo.commit(
        {"policy.py": source, "another_codec.py": growing, "field_codec.py": growing}
    )
    status, report = repo.compare(head, moved, BuiltinHandlerTypeSwitch)
    assert status == 1  # A filename suffix cannot admit another primitive dispatcher.
    assert report["head"][BuiltinHandlerTypeSwitch.at(f"{repo.root}/field_codec.py").text] == 0
    assert report["delta"][BuiltinHandlerTypeSwitch.at(f"{repo.root}/another_codec.py").text] == 3


def owner(name: str, lines: int, indent: str = "") -> str:
    return (
        indent
        + f"class {name}:\n"
        + "".join(indent + f"    field_{index} = {index}\n" for index in range(lines - 1))
    )


def module(*owners: str) -> str:
    """The owners as one module in Black's form, so the formatting ratchet counts nothing and
    each comparison measures only the classes."""
    import black

    return black.format_str("".join(owners), mode=black.Mode())


@pytest.mark.parametrize("root", ["src/agent_comms", "src/toad", "src/textual"])
def test_family_flattening_cannot_offset_another_file_or_admit_a_named_facade(
    tmp_path: Path, root: str
):
    repo = Repository(tmp_path, root)
    from audit.measures import FamilyFlattened as AuditFamilyFlattened

    original = "def carry(value):\n    return (value.declared_name, value.family_name)\n"
    base = repo.commit({"first.py": original, "second.py": "value = 1\n"})
    head = repo.commit(
        {
            "first.py": "def carry(value):\n    return value\n",
            "second.py": "def carry(value):\n    return value.declared_name\n",
        }
    )
    status, report = repo.compare(base, head, FamilyFlattened)
    assert status == 1
    assert report["delta"][FamilyFlattened.at(f"{repo.root}/first.py").text] == -2
    assert report["delta"][FamilyFlattened.at(f"{repo.root}/second.py").text] == 1
    assert FamilyFlattened.count(original.encode(), "consumer.py") == (
        AuditFamilyFlattened.count(ast.parse(original))
    )
    original_modules = tuple(
        module for module in FamilyFlattened.mechanism_modules if module.parent == Path(repo.root)
    )
    facades = {
        f"{directory}/{module.parent.name}/{module.name}"
        for module in FamilyFlattened.mechanism_modules
        for directory in ("nested", "vendor")
    }
    facades.update(f"nested/{module.name}" for module in FamilyFlattened.mechanism_modules)
    moved = repo.commit(
        {
            "second.py": "def carry(value):\n    return value\n",
            "another_codec.py": original,
            **{module.name: original for module in original_modules},
            **{path: original for path in facades},
        }
    )
    status, report = repo.compare(head, moved, FamilyFlattened)
    assert status == 1
    assert report["delta"][FamilyFlattened.at(f"{repo.root}/another_codec.py").text] == 2
    for module in original_modules:
        assert report["head"][FamilyFlattened.at(module.as_posix()).text] == 0
    for path in facades:
        assert report["head"][FamilyFlattened.at(f"{repo.root}/{path}").text] == 2


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("class Owner:\n def name(self):\n  return self.declared_name\n", 0),
        ("class Owner:\n @classmethod\n def name(cls):\n  return cls.declared_name\n", 0),
        ("class Owner:\n @classmethod\n def name(cls):\n  return Owner.declared_name\n", 0),
        ("class Owner:\n def name(self):\n  return Other.declared_name\n", 1),
        ("def name(value):\n return value.declared_name\n", 1),
        ("def name(value):\n return FieldCodec.decode(str, FieldCodec.encode(value))\n", 1),
        (
            "class Owner:\n @classmethod\n def name(cls):\n  return FieldCodec.decode(str, FieldCodec.encode(cls))\n",
            1,
        ),
        (
            "def name(value):\n return FieldCodec.decode(str, FieldCodec.encode(value.declared_name))\n",
            1,
        ),
        ("def name(value):\n return FieldCodec.decode(str, value)\n", 0),
        (
            "class Owner:\n def name(self, other):\n  return (self.declared_name, other.declared_name)\n",
            1,
        ),
        ("if __name__ == '__main__':\n    main()\n", 0),
        ("def name(value):\n return value.name == 'released'\n", 1),
    ],
)
def test_family_name_owner_reads_and_codec_detours(source: str, expected: int):

    assert FamilyFlattened.count(source.encode(), "consumer.py") == expected


def test_small_owners_stay_within_the_class_threshold(repo: Repository) -> None:
    base = repo.commit({"owners.py": owner("Small", 2)})
    head = repo.commit({"owners.py": module(owner("Small", 500), owner("New", 500))})
    status, report = repo.compare(base, head, GodClassExcess)
    assert status == 0
    assert report["delta"][GodClassExcess.at(f"{repo.root}/owners.py").member("Small").text] == 0
    assert report["delta"][GodClassExcess.at(f"{repo.root}/owners.py").member("New").text] == 0


def test_class_growth_cannot_be_cancelled_by_another_class(repo: Repository) -> None:
    base = repo.commit({"classes.py": module(owner("Growing", 501), owner("Shrinking", 600))})
    head = repo.commit({"classes.py": module(owner("Growing", 502), owner("Shrinking", 500))})
    status, report = repo.compare(base, head, GodClassExcess)
    assert status == 1
    assert report["delta"][GodClassExcess.at(f"{repo.root}/classes.py").member("Growing").text] == 1
    assert report["delta"][GodClassExcess.at(f"{repo.root}/classes.py").member("Shrinking").text] == -100


@pytest.mark.parametrize("before", [0, 500])
def test_new_or_crossing_god_class_is_refused(repo: Repository, before: int) -> None:
    base = repo.commit({"owners.py": owner("New", before)} if before else {})
    head = repo.commit({"owners.py": owner("New", 501)})
    status, report = repo.compare(base, head, GodClassExcess)
    assert status == 1
    assert report["base"][GodClassExcess.at(f"{repo.root}/owners.py").member("New").text] == 0
    assert report["delta"][GodClassExcess.at(f"{repo.root}/owners.py").member("New").text] == 1


def test_class_move_keeps_baseline_and_new_small_owner_has_no_class_debt(repo: Repository) -> None:
    base = repo.commit({"old.py": owner("Existing", 501)})
    moved = repo.commit({"old.py": None, "new.py": module(owner("Existing", 501), owner("NewOwner", 25))})
    status, report = repo.compare(base, moved, GodClassExcess)
    assert status == 0
    assert report["base"][GodClassExcess.at(f"{repo.root}/new.py").member("Existing").text] == 1
    assert report["delta"][GodClassExcess.at(f"{repo.root}/new.py").member("Existing").text] == 0
    assert report["delta"][GodClassExcess.at(f"{repo.root}/new.py").member("NewOwner").text] == 0
    grown = repo.commit({"new.py": module(owner("Existing", 502), owner("NewOwner", 26))})
    assert repo.compare(moved, grown, GodClassExcess)[0] == 1


def test_moved_class_growth_is_rejected(repo: Repository) -> None:
    base = repo.commit({"old.py": owner("Existing", 500)})
    head = repo.commit({"old.py": None, "new.py": owner("Existing", 501)})
    status, report = repo.compare(base, head, GodClassExcess)
    assert status == 1
    assert report["delta"][GodClassExcess.at(f"{repo.root}/new.py").member("Existing").text] == 1


def test_formatting_a_god_class_is_not_growth(repo: Repository) -> None:
    import black

    wide = "    wide = combine(" + ", ".join(f"argument_{index}" for index in range(30)) + ")\n"
    unformatted = owner("Wide", 501) + wide
    formatted = black.format_str(unformatted, mode=black.Mode())
    assert formatted.count("\n") > unformatted.count("\n")
    base = repo.commit({"wide.py": unformatted})
    head = repo.commit({"wide.py": formatted})
    status, report = repo.compare(base, head, GodClassExcess)
    assert report["delta"][GodClassExcess.at(f"{repo.root}/wide.py").member("Wide").text] == 0
    assert status == 0


def test_nested_and_duplicate_named_classes_keep_separate_baselines(repo: Repository) -> None:
    nested = "class Outer:\n" + owner("Inner", 501, "    ")
    base = repo.commit({"one.py": nested, "two.py": owner("Inner", 500)})
    head = repo.commit({"one.py": nested, "two.py": owner("Inner", 501)})
    status, report = repo.compare(base, head, GodClassExcess)
    assert status == 1
    assert report["delta"][GodClassExcess.at(f"{repo.root}/one.py::Outer.Inner").text] == 0
    assert report["delta"][GodClassExcess.at(f"{repo.root}/two.py").member("Inner").text] == 1


def test_chain_terms_catch_growth_hidden_by_unchanged_chain_count(repo: Repository) -> None:
    base = repo.commit({"rules.py": "valid = a and b and c and d\n"})
    head = repo.commit({"rules.py": "valid = a and b and c and d and e\n"})
    status, report = repo.compare(base, head, BooleanChainTerms, LongBooleanChain)
    assert status == 1
    assert report["delta"]["LongBooleanChain"] == 0
    assert report["delta"][BooleanChainTerms.at(f"{repo.root}/rules.py").text] == 1


@pytest.mark.parametrize(
    ("measure", "arms", "decision"),
    [
        (StringDispatch, StringDispatchArms, "if value == {case!r}: return 1"),
        (StringDispatch, StringDispatchArms, "if value in ({case!r},): return 1"),
        (StringDispatch, StringDispatchArms, "match value:\n        case {case!r}: return 1"),
        (StringDispatch, StringDispatchArms, "match value:\n        case {case!r} | {case!r}: return 1"),
        (StringDispatch, StringDispatchArms, "match value:\n        case ({case!r} | {case!r}) as named: return 1"),
        (TypeSwitch, TypeSwitchArms, "if isinstance(value, Case{case}): return 1"),
        (TypeSwitch, TypeSwitchArms, "if type(value) is Case{case}: return 1"),
        (TypeSwitch, TypeSwitchArms, "match value:\n        case Case{case}(): return 1"),
        (TypeSwitch, TypeSwitchArms, "match value:\n        case Case{case}() as named: return 1"),
        (TypeSwitch, TypeSwitchArms, "if issubclass(value, Case{case}): return 1"),
        (TypeSwitch, TypeSwitchArms, "if value is Case{case}: return 1"),
    ],
)
def test_dispatch_growth_and_reduction_use_the_installed_command(
    repo: Repository, measure: type[Measure], arms: type[Measure], decision: str
) -> None:
    def source(cases):
        return (
            "def decide(value):\n"
            + "\n".join("    " + decision.format(case=case) for case in cases)
            + "\n"
        )

    before = repo.commit({"policy.py": source(range(3))})
    grown = repo.commit({"policy.py": source(range(4))})
    status, report = repo.compare(before, grown, measure, arms)
    assert status == 1
    assert report["delta"][measure.at(f"{repo.root}/policy.py").text] == 0
    assert report["delta"][arms.at(f"{repo.root}/policy.py").text] == 1
    reduced = repo.commit({"policy.py": source(range(2))})
    status, report = repo.compare(grown, reduced, measure, arms)
    assert status == 0
    assert report["delta"][measure.at(f"{repo.root}/policy.py").text] == -1
    assert report["delta"][arms.at(f"{repo.root}/policy.py").text] == -4


def test_own_class_type_test_counts_one_arm_and_never_a_foreign_class(repo: Repository) -> None:
    owned = "class Case:\n    pass\n\n\nclass Import:\n    pass\n"
    base = repo.commit({"owned.py": owned, "check.py": "import ast\n\n\ndef read(node):\n"
                        "    return isinstance(node, ast.Import)\n"})
    head = repo.commit({"check.py": "import ast\n\nfrom .owned import Case\n\n\ndef read(node):\n"
                        "    return isinstance(node, ast.Import) or issubclass(node, Case)\n"})
    status, report = repo.compare(base, head, OwnTypeTest)
    assert status == 1
    assert report["base"][OwnTypeTest.at(f"{repo.root}/check.py").text] == 0
    assert report["delta"][OwnTypeTest.at(f"{repo.root}/check.py").text] == 1


def test_dispatch_scopes_subjects_and_repeated_cases_do_not_combine(repo: Repository) -> None:
    before = repo.commit({})
    head = repo.commit(
        {
            "scope.py": """
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
"""
        }
    )
    status, report = repo.compare(before, head, FamilyFlattened, StringDispatch, StringDispatchArms)
    # This checks the three-arm dispatch measure's scope. The separate
    # family measure also reports these literal comparisons.
    assert status == 1
    assert report["head"][FamilyFlattened.at(f"{repo.root}/scope.py").text] > 0
    assert report["head"][StringDispatch.at(f"{repo.root}/scope.py").text] == 0
    assert report["head"][StringDispatchArms.at(f"{repo.root}/scope.py").text] == 0


@pytest.mark.parametrize(
    ("measure", "source"),
    [
        (BooleanChainTerms, "valid = a and b and c and d\n"),
        (ForeignAbsenceProbe, "absent = other.value is None\n"),
        (CodecSubclass, "class Local(FieldCodec):\n    pass\n"),
        (StringDispatch, 'def decide(x):\n    return x in ("a", "b", "c")\n'),
        (
            TypeSwitch,
            "def decide(x):\n    return isinstance(x, A) or isinstance(x, B) or isinstance(x, C)\n",
        ),
        (OwnTypeTest, "class Case:\n    pass\ndef read(x):\n    return isinstance(x, Case)\n"),
        (AnyName, "from typing import Any\ndef read(value: Any) -> None: ...\n"),
        (AnyName, "from typing import Any\nvalue = Any\n"),
        (UnannotatedParameter, "def read(value): ...\n"),
        (TypeErrors, 'value: int = "text"\n'),
    ],
)
def test_per_file_increase_cannot_be_offset_or_moved(
    repo: Repository, measure: type[Measure], source: str
) -> None:
    base = repo.commit({"old.py": source, "new.py": ""})
    head = repo.commit({"old.py": None, "new.py": source})
    status, report = repo.compare(base, head, measure)
    assert status == 1
    assert report["delta"][measure.at(f"{repo.root}/new.py").text] > 0
    assert (
        sum(report["delta"].get(measure.at(f"{repo.root}/{name}").text, 0)
            for name in ("old.py", "new.py")) == 0
    )


@pytest.mark.parametrize(
    ("measure", "source"), [(Unformatted, "value=1\n"), (LintFindings, "import os\n")]
)
def test_advisory_growth_is_measured_and_warned_never_failed(
    repo: Repository, measure: type[Measure], source: str
) -> None:
    base = repo.commit({"new.py": ""})
    head = repo.commit({"new.py": source})
    status, report = repo.compare(base, head, measure)
    count = measure.at(f"{repo.root}/new.py")
    assert status == 0
    assert report["delta"][count.text] > 0
    assert measure.grew(count, 1).startswith(f"::warning file={repo.root}/new.py::")


def test_a_renamed_file_compares_with_its_old_path(repo: Repository) -> None:
    kept = "".join(f"def keep{i}(value: int) -> int:\n    return value\n" for i in range(8))
    source = kept + "def read(value): ...\n"
    base = repo.commit({"old.py": source})
    moved = repo.commit({"old.py": None, "nested/new.py": source})
    status, report = repo.compare(base, moved, UnannotatedParameter)
    assert status == 0
    assert report["delta"][UnannotatedParameter.at(f"{repo.root}/nested/new.py").text] == 0
    assert UnannotatedParameter.at(f"{repo.root}/old.py").text not in report["delta"]
    grown = repo.commit({"nested/new.py": source + "def write(value): ...\n"})
    status, report = repo.compare(base, grown, UnannotatedParameter)
    assert status == 1
    assert report["delta"][UnannotatedParameter.at(f"{repo.root}/nested/new.py").text] == 1


def test_both_revisions_are_checked_under_the_head_configuration(repo: Repository) -> None:
    source = "import sys\nimport os\n\nvalue: int = 'text'\nused = (os, sys)\n"
    (repo.path / "pyproject.toml").write_text(
        '[tool.ruff.lint]\nselect = ["E", "F"]\n[tool.mypy]\npython_version = "3.11"\n'
    )
    base = repo.commit({"kept.py": source + "type Pair = tuple[int, int]\n"})
    (repo.path / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["E", "F", "I"]\n')
    head = repo.commit({"kept.py": source + "type Pair = tuple[int, int]\nother = 1\n"})
    status, report = repo.compare(base, head, LintFindings, TypeErrors, Unformatted)
    # The base alone cannot parse PEP 695 under its own 3.11 target, and the head adds the
    # import-order rule; under one configuration neither changes the file's counts.
    assert report["base"][TypeErrors.at(f"{repo.root}/kept.py").text] == 1
    assert report["base"][LintFindings.at(f"{repo.root}/kept.py").text] == 1
    for measure in (TypeErrors, LintFindings, Unformatted):
        assert report["delta"][measure.at(f"{repo.root}/kept.py").text] == 0


def test_foreign_probes_and_codec_bases_use_ast_not_text(repo: Repository) -> None:
    base = repo.commit({})
    head = repo.commit(
        {
            "boundary.py": """
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
"""
        }
    )
    status, report = repo.compare(base, head, CodecSubclass, ForeignAbsenceProbe)
    assert status == 1
    assert report["head"][ForeignAbsenceProbe.at(f"{repo.root}/boundary.py").text] == 2
    assert report["head"][CodecSubclass.at(f"{repo.root}/boundary.py").text] == 1


def test_connection_boolean_is_not_an_absence_probe(repo: Repository) -> None:
    base = repo.commit({})
    boolean = repo.commit(
        {
            "connection.py": """
from multiprocessing.connection import Connection

class Reader:
    connection: Connection

    def readable(self):
        return not self.connection.closed
"""
        }
    )
    status, report = repo.compare(base, boolean, ForeignAbsenceProbe)
    assert status == 0
    assert report["head"][ForeignAbsenceProbe.at(f"{repo.root}/connection.py").text] == 0

    missing = repo.commit(
        {
            "missing.py": """
class State:
    value: str | None

def missing(state: State):
    return state.value is None

def present(state: State):
    return state.value is not None
"""
        }
    )
    status, report = repo.compare(boolean, missing, ForeignAbsenceProbe)
    assert status == 1
    assert report["delta"][ForeignAbsenceProbe.at(f"{repo.root}/missing.py").text] == 2


def test_type_expression_boundary_keeps_runtime_dictionary_access(repo: Repository) -> None:
    source = (
        "\n".join(
            [
                "import typing as t",
                "from typing import Literal as Choice",
                'transport: Choice["stdio"]',
                "class Declaration:",
                '    transport: t.Literal["stdio"]',
                'def render(value: Choice["stdio"]) -> t.Literal["stdio"]:',
                '    local: Choice["stdio"]',
                '    return "stdio"',
                'async def asynchronous(value: Choice["stdio"]) -> Choice["stdio"]:',
                '    return "stdio"',
            ]
        )
        + "\n"
    )
    if hasattr(ast, "TypeAlias"):
        source += 'type Transport = t.Literal["stdio"]\n'
        source += 'def generic[T: t.Literal["stdio"]](value: T) -> T:\n    return value\n'
    base = repo.commit({"boundary.py": ""})
    declarations = repo.commit({"boundary.py": source})
    status, report = repo.compare(base, declarations, StringSubscript)
    assert status == 0
    assert report["head"]["StringSubscript"] == 0
    # Annotation spelling cannot conceal actual initializer/default/decorator/body reads.
    runtime = (
        source
        + "\n".join(
            [
                'transport: Choice["stdio"] = payload["transport"]',
                'dynamic_type: schema["type"]',
                '@decorators["render"]',
                'def read(value: Choice["stdio"] = defaults["transport"]) -> Choice["stdio"]:',
                '    return payload["transport"]',
            ]
        )
        + "\n"
    )
    changed = repo.commit({"boundary.py": runtime})
    status, report = repo.compare(declarations, changed, StringSubscript)
    assert status == 1
    assert report["delta"]["StringSubscript"] == 5


def test_typing_spelling_does_not_hide_shadowed_or_unresolved_read(repo: Repository) -> None:
    base = repo.commit({"shadow.py": ""})
    head = repo.commit(
        {
            "shadow.py": "\n".join(
                [
                    "from typing import Literal",
                    "def read(Literal):",
                    '    return Literal["value"]',
                    'unknown: schema["type"]',
                ]
            )
            + "\n"
        }
    )
    status, report = repo.compare(base, head, StringSubscript)
    assert status == 1
    assert report["delta"]["StringSubscript"] == 2


ANY_DECLARATIONS = (
    "\n".join(
        [
            "import typing as t",
            "from dataclasses import dataclass",
            "from typing import TYPE_CHECKING, Annotated, Callable, Literal, Optional, TypeAlias",
            "if TYPE_CHECKING:",
            "    from typing import Any",
            "Payload = dict[str, Any]",
            "Row: TypeAlias = list[Any]",
            "@dataclass(frozen=True)",
            "class Record:",
            "    fill: Any = 0",
            '    quoted: "Optional[Any]" = None',
            "class Plain:",
            "    cache: t.Any",
            "    def read(self, value: Any | None, *rest: Any, **named: Any) -> Callable[..., Any]:",
            '        local: Annotated[Any, "Any"] = value',
            "        return local",
        ]
    )
    + "\n"
)

# Loaded Any references count even outside a declaration; literal strings do not.
ANY_NON_DECLARATIONS = (
    "\n".join(
        [
            "from typing import Annotated, Any, Literal",
            'choice: Literal["Any"]',
            "described: Annotated[int, Any]",
            "skipped = (Any, object)",
            'chosen = handlers["Any"]',
            "def unstated(annotation: object) -> bool:",
            "    return annotation is Any or isinstance(annotation, str)",
            "class Local:",
            "    Any = int",
        ]
    )
    + "\n"
)


def test_any_is_counted_once_in_declarations_and_values(repo: Repository) -> None:

    uses = AnyName.uses(ANY_DECLARATIONS.encode(), "declarations.py")
    assert sorted((use.site, use.owner) for use in uses) == [
        ("alias", ""),
        ("alias", ""),
        ("class attribute", "Plain"),
        ("dataclass field", "Record"),
        ("dataclass field", "Record"),
        ("parameter", "Plain.read"),
        ("parameter", "Plain.read"),
        ("parameter", "Plain.read"),
        ("return", "Plain.read"),
        ("variable", "Plain.read"),
    ]
    assert AnyName.count(ANY_NON_DECLARATIONS.encode(), "values.py") == 3

    base = repo.commit({"declarations.py": "", "values.py": ""})
    head = repo.commit({"declarations.py": ANY_DECLARATIONS, "values.py": ANY_NON_DECLARATIONS})
    status, report = repo.compare(base, head, AnyName)
    assert status == 1
    assert report["delta"][AnyName.at(f"{repo.root}/declarations.py").text] == len(uses)
    assert report["delta"][AnyName.at(f"{repo.root}/values.py").text] == 3


def test_stating_a_type_lowers_the_any_count_and_passes(repo: Repository) -> None:
    base = repo.commit({"declarations.py": ANY_DECLARATIONS})
    head = repo.commit(
        {"declarations.py": ANY_DECLARATIONS.replace("fill: Any = 0", "fill: int = 0")}
    )
    status, report = repo.compare(base, head, AnyName)
    assert status == 0
    assert report["delta"][AnyName.at(f"{repo.root}/declarations.py").text] == -1


@pytest.mark.parametrize(
    ("measure", "deleted", "source"),
    [
        (FrozenFieldWrite, 'object.__setattr__(self, "old", 1)\n',
         'object.__setattr__(self, "value", 1)\n'),
        (BannedVocabulary, "old_receipt = 1\n", "pending_receipt = 1\n"),
    ],
)
def test_clean_changed_lines_cannot_be_offset_with_a_deletion(repo, measure, deleted, source):
    """Deleting one occurrence never pays for writing a different one; the same occurrence
    leaving one module for another is a move
    (test_an_absence_moved_with_its_class_to_another_module_is_not_written)."""
    base = repo.commit({"old.py": deleted, "new.py": ""})
    head = repo.commit({"old.py": None, "new.py": source})
    status, report = repo.compare(base, head, measure)
    identity = measure.at(f"{repo.root}/new.py").text
    assert status == 1
    assert report["delta"][identity] == 1
    assert len(report["introduced"][identity]) == 1


@pytest.mark.parametrize(
    ("measure", "source"),
    [
        (FrozenFieldWrite, 'object.__setattr__(self, "value", 1)\n'),
        (BannedVocabulary, "pending_receipt = 1\n"),
    ],
)
def test_unchanged_old_sites_are_not_forced_to_zero(repo, measure, source):
    base = repo.commit({"old.py": source + "clean = 1\n"})
    head = repo.commit({"old.py": source + "clean = 2\n"})
    status, report = repo.compare(base, head, measure)
    identity = measure.at(f"{repo.root}/old.py").text
    assert status == 0
    assert report["head"][identity] == 1
    assert report["introduced"][identity] == []


def test_none_as_absence_fails_with_its_line_except_in_a_declared_boundary_decoder(repo):
    marked = (
        "from agent_comms.field_codec import boundary_decoder\n\n\n"
        "@boundary_decoder\ndef decoded(value: str):\n    return value\n"
    )
    files = {
        "plan.py": marked,
        "foreign.py": marked.replace("agent_comms.field_codec", "elsewhere"),
        "owner.py": "def owner(value: str):\n    return value\n",
    }
    base = repo.commit(files)
    head = repo.commit(
        {name: source.replace("value: str", "value: str | None") for name, source in files.items()}
    )
    status, report = repo.compare(base, head, NoneAbsence)
    assert status == 1
    assert {identity: lines for identity, lines in report["introduced"].items() if lines} == {
        NoneAbsence.at(f"{repo.root}/owner.py").text: [1],
        NoneAbsence.at(f"{repo.root}/foreign.py").text: [5],
    }
    plan = NoneAbsence.at(f"{repo.root}/plan.py").text
    assert report["introduced"][plan] == [] and report["delta"][plan] == 0


def test_an_unchanged_absence_on_a_rewritten_line_is_not_written_but_a_new_one_is(repo):
    base = repo.commit({"owner.py": "def ready(stored):\n    return stored is not None and stored.done\n"})
    kept = repo.commit({"owner.py": "def ready(stored):\n    return stored is not None and stored.settled()\n"})
    status, report = repo.compare(base, kept, NoneAbsence)
    identity = NoneAbsence.at(f"{repo.root}/owner.py").text
    assert report["introduced"][identity] == []
    added = repo.commit(
        {"owner.py": "def ready(stored):\n    return stored is not None and stored.owner is not None\n"}
    )
    status, report = repo.compare(kept, added, NoneAbsence)
    assert status == 1
    assert report["introduced"][identity] == [2]


def test_an_absence_moved_with_its_class_to_another_module_is_not_written(repo):
    holder = "class Holder:\n    def check(self, v):\n        return v is None\n"
    other = "class Other:\n    def check(self, v):\n        return v is None\n"
    base = repo.commit({"attempts.py": holder, "executions.py": "VALUE = 1\n"})
    moved = repo.commit({"attempts.py": "VALUE = 2\n", "executions.py": holder})
    status, report = repo.compare(base, moved, NoneAbsence)
    assert report["introduced"][NoneAbsence.at(f"{repo.root}/executions.py").text] == []
    deleted = repo.commit({"attempts.py": holder, "executions.py": None})
    moved_out = repo.commit({"attempts.py": None, "owners.py": holder + "\n\n" + other})
    status, report = repo.compare(deleted, moved_out, NoneAbsence)
    assert status == 1
    assert report["introduced"][NoneAbsence.at(f"{repo.root}/owners.py").text] == [8]


def test_git_context_configuration_cannot_mark_unchanged_lines_as_new(repo):
    repo.git("config", "diff.interHunkContext", "3")
    base = repo.commit({"names.py": "first = 1\nold_receipt = 1\nlast = 1\n"})
    head = repo.commit({"names.py": "first = 2\nold_receipt = 1\nlast = 2\n"})
    status, report = repo.compare(base, head, BannedVocabulary)
    identity = BannedVocabulary.at(f"{repo.root}/names.py").text
    assert status == 0
    assert report["head"][identity] == 1
    assert report["introduced"][identity] == []


@pytest.mark.parametrize("word", BannedVocabulary.words)
def test_net_reduction_does_not_pay_for_a_new_identifier(repo, word):
    base = repo.commit({"names.py": "old_admit = 1\nold_receipt = 2\n"})
    head = repo.commit({"names.py": f"new_{word} = 3\n"})
    status, report = repo.compare(base, head, BannedVocabulary)
    identity = BannedVocabulary.at(f"{repo.root}/names.py").text
    assert status == 1
    assert report["base"][identity] == 2
    assert report["head"][identity] == 1
    assert len(report["introduced"][identity]) == 1


@pytest.mark.parametrize("word", BannedVocabulary.words)
def test_old_function_name_and_multiline_attribute_have_their_own_lines(repo, word):
    base = repo.commit(
        {"names.py": f"def old_admit():\n    return (\n        peer\n        .{word}\n    )\n"}
    )
    head = repo.commit(
        {"names.py": f"def old_admit():\n    return (\n        other\n        .{word}\n    )\n"}
    )
    status, report = repo.compare(base, head, BannedVocabulary)
    identity = BannedVocabulary.at(f"{repo.root}/names.py").text
    assert status == 0
    assert report["head"][identity] == 2
    assert report["introduced"][identity] == []


def test_changed_argument_still_changes_a_multiline_frozen_write(repo):
    base = repo.commit({"owner.py": 'object.__setattr__(\n    self, "value",\n    1,\n)\n'})
    head = repo.commit({"owner.py": 'object.__setattr__(\n    self, "value",\n    2,\n)\n'})
    status, report = repo.compare(base, head, FrozenFieldWrite)
    identity = FrozenFieldWrite.at(f"{repo.root}/owner.py").text
    assert status == 1
    assert report["delta"][identity] == 0
    assert len(report["introduced"][identity]) == 1


@pytest.mark.parametrize("word", BannedVocabulary.words)
def test_vocabulary_includes_identifiers_comments_and_docstring_lines_only(word):
    source = f'''"""Receipt and {word} share one prose line.\nAdmitted work.\n"""\n# admission\nkey = "admission_generation"\ndef admit_receipt({word.title()}, **clean):\n    return peer.receipt({word.title()}, admission=1)\n'''.encode()
    # Three prose lines; function, parameter, attribute, loaded parameter and keyword.
    assert BannedVocabulary.count(source, "values.py") == 8
    assert FrozenFieldWrite.count(b'text = "object.__setattr__(x, y, z)"\n', "values.py") == 0


def test_a_frozen_write_counts_except_in_its_own_classes_post_init():
    source = b"""class Snapshot:
    def __post_init__(self):
        object.__setattr__(self, "maps", dict(self.maps))
        object.__setattr__(self.other, "maps", {})
        def later():
            object.__setattr__(self, "maps", {})
    def refresh(self):
        object.__setattr__(self, "maps", {})
def __post_init__(self):
    object.__setattr__(self, "maps", {})
"""
    # Only the first write builds the instance; another object, a nested scope, another
    # method and a module-level function of the same name all still count.
    assert FrozenFieldWrite.count(source, "values.py") == 4


def test_changed_prose_fails_but_an_unchanged_disk_key_does_not(repo):
    original = 'key = "admission_generation"\n# clean wording\n'
    base = repo.commit({"words.py": original})
    head = repo.commit({"words.py": original.replace("clean wording", "admission wording")})
    status, report = repo.compare(base, head, BannedVocabulary)
    identity = BannedVocabulary.at(f"{repo.root}/words.py").text
    assert status == 1
    assert report["head"][identity] == 1
    assert len(report["introduced"][identity]) == 1


def test_local_import_scope_counts_nested_functions_and_classes_once():
    source = b"""import top\nclass ModuleClass:\n    import top_class\ndef outer():\n    import one\n    class Local:\n        import two\n        def method(self):\n            from package import three\n    async def inner():\n        import four\n"""
    assert FunctionLocalImport.count(source, "imports.py") == 4


def test_a_new_enum_fails_the_ratchet_and_folding_one_passes(repo):
    plain = "class Family:\n    pass\n"
    base = repo.commit({"kinds.py": plain})
    grown = repo.commit({"kinds.py": plain + "import enum\nclass Kind(enum.StrEnum):\n    A = 'a'\n"})
    status, report = repo.compare(base, grown, EnumDeclaration)
    identity = EnumDeclaration.at(f"{repo.root}/kinds.py").text
    assert status == 1
    assert report["head"][identity] == 1
    folded = repo.commit({"kinds.py": plain})
    assert repo.compare(grown, folded, EnumDeclaration)[0] == 0


def test_enum_declarations_count_every_stdlib_enum_base_and_nothing_else():
    source = b"""import enum\nfrom enum import Enum, StrEnum\nclass A(Enum): pass\nclass B(enum.IntFlag): pass\nclass C(str, StrEnum): pass\nclass Family: pass\nclass Member(Family): pass\ndef local():\n    class D(enum.Flag): pass\n"""
    assert EnumDeclaration.lines(source, "kinds.py") == [3, 4, 5, 9]


def string_identity_readings(source: bytes) -> list[tuple[str, type]]:
    declared = b"class CarryStep: pass\nclass Claimed: pass\ndef carry_hunts(): pass\n"
    spellings = DeclaredSpellings.read(
        {
            path: DeclaredSpellings.declarations(path, module)
            for path, module in [
                ("tools/carry_hunts.py", declared),
                ("src/pkg/__init__.py", b""),
                ("src/pkg/route_table.py", b""),
                ("tools/use.py", source),
            ]
        }
    )
    return [(site.spelling, site.reading) for site in spellings.sites(source, "tools/use.py")]


def test_string_identity_reads_declared_names_compared_kinds_and_named_access():
    source = b"""after = "carry_hunts"
step = "CarryStep"
table = "pkg.route_table"
loaded = sys.modules["pkg.route_table"]
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
"""
    assert string_identity_readings(source) == [
        ("carry_hunts", NamedDeclaration),
        ("CarryStep", NamedDeclaration),
        ("pkg.route_table", NamedDeclaration),
        ("release", ComparedKind),
        ("fast", ComparedKind),
        ("slow", ComparedKind),
        ("square", ComparedKind),
        ("model", NamedAccess),
        ("pkg.plugin", NamedAccess),
        ("text", NamedAccess),
    ]


def test_a_one_word_class_name_counts_only_where_a_type_is_expected():
    source = b"""notice = Message(owner, owner, "Claimed", kind)
label = "Claimed"
field: Annotated[int, tool_field("Claimed")] = 0
step = "CarryStep"
def read() -> "Claimed": pass
held: "Claimed"
class Index(list["Claimed"]): pass
Bound = TypeVar("Bound", bound="Claimed")
"""
    assert string_identity_readings(source) == [
        ("CarryStep", NamedDeclaration),
        ("Claimed", NamedDeclaration),
        ("Claimed", NamedDeclaration),
        ("Claimed", NamedDeclaration),
        ("Claimed", NamedDeclaration),
    ]


def test_a_module_name_counts_only_where_a_string_selects_a_module():
    source = b"""migrate = numbers and "route_table" in stored
path = "pkg.route_table"
spec = importlib.util.find_spec("pkg.route_table")
cached = sys.modules.get("pkg.route_table")
present = "pkg.route_table" in sys.modules
skills = files("pkg.route_table") / "skills"
command = (interpreter, "-m", "pkg.route_table", root)
"""
    assert string_identity_readings(source) == [
        ("pkg.route_table", NamedDeclaration),
        ("pkg.route_table", NamedDeclaration),
        ("pkg.route_table", NamedDeclaration),
        ("pkg.route_table", NamedDeclaration),
        ("pkg.route_table", NamedDeclaration),
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


def test_string_identity_measures_spellings_outside_root_and_skips_declared_tests(
    repo: Repository,
) -> None:
    (repo.path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    outside = {
        "tools/carry_other.py": "def run(): pass\n",
        "tools/step.py": "after = None\n",
        "tests/test_step.py": "after = None\n",
    }
    for name, source in outside.items():
        (repo.path / name).parent.mkdir(parents=True, exist_ok=True)
        (repo.path / name).write_text(source)
    base = repo.commit({"kept.py": "value = 1\n"})
    for name in ("tools/step.py", "tests/test_step.py"):
        (repo.path / name).write_text('after = sys.modules["carry_other"]\n')
    head = repo.commit({})
    status, report = repo.compare(base, head, StringIdentity)
    assert status == 1
    assert {key: value for key, value in report["delta"].items() if value} == {
        StringIdentity.at("tools/step.py").text: 1
    }
    assert StringIdentity.at("tests/test_step.py").text not in report["head"]


def test_broad_suppress_is_counted_and_cannot_be_added(repo):
    source = b"""with suppress(OSError): work()
with contextlib.suppress(ConnectionError, TimeoutError): work()
with suppress(FileNotFoundError): absent()
with suppress(Exception): raise RuntimeError()
"""
    assert BroadSwallow.count(source, "read.py") == 3
    before = repo.commit({"reader.py": "work()\n"})
    after = repo.commit({"reader.py": "with suppress(OSError): work()\n"})
    status, report = repo.compare(before, after, BroadSwallow)
    identity = BroadSwallow.at(f"{repo.root}/reader.py").text
    assert status == 1 and len(report["introduced"][identity]) == 1
    assert report["delta"][identity] == 1


def test_broad_handler_carrying_its_error_into_a_typed_outcome_is_not_a_swallow():
    source = b"""for pid in pids:
    try: read(pid)
    except (OSError, IdentityMismatchError) as error:
        outcomes.append(UnreadableRuntimeProcess(pid, str(error)))
        continue
    try: read(pid)
    except OSError as error:
        log.warning(str(error))
        continue
    try: read(pid)
    except OSError as error:
        return Missing()
"""
    assert BroadSwallow.count(source, "read.py") == 2


def test_field_spelling_rosters_count_loops_not_declarations():
    source = b"""fields = ("path", "file_path", "filePath")\nfor key in ("path", "file_path", "filePath"):\n    value = data[key]\nvalue = next(data[key] for key in ("path", "file_path", "filePath") if key in data)\n"""
    assert ForeignFieldSpellings.count(source, "read.py") == 2
    assert ForeignFieldSpellings.lines(source, "read.py") == [2, 4]


def test_broad_swallow_is_counted_and_cannot_be_added(repo):
    source = b"""try: work()
except (OSError, ValueError): pass
try: work()
except Exception: return_default()
try: work()
except: continue_work()
try: work()
except RuntimeError: raise
try: work()
except FileNotFoundError: absent()
"""
    assert BroadSwallow.count(source, "read.py") == 3
    root = Path(__file__).resolve().parents[2] / "src/agent_comms"
    assert (
        sum(BroadSwallow.count(path.read_bytes(), str(path)) for path in root.rglob("*.py")) <= 29
    )
    before = repo.commit({"reader.py": "work()\n"})
    after = repo.commit({"reader.py": "try: work()\nexcept OSError: pass\n"})
    status, report = repo.compare(before, after, BroadSwallow)
    identity = BroadSwallow.at(f"{repo.root}/reader.py").text
    assert status == 1 and len(report["introduced"][identity]) == 1
    assert report["delta"][identity] == 1


def test_broad_handlers_only_count_defaults_returned_by_the_handler():
    source = b"""def read():\n    try: work()\n    except Exception:\n        return None\n    try: work()\n    except:\n        if condition: return {}\n    try: work()\n    except OSError:\n        return None\n    try: work()\n    except BaseException:\n        raise\n    try: work()\n    except Exception:\n        def helper(): return None\n        raise\n"""
    assert BroadExceptDefault.count(source, "read.py") == 2


def test_growth_lines_end_the_merged_log(repo: Repository) -> None:
    """run-step.sh annotates the last lines of stdout and stderr merged into one pipe; the
    report is flushed first, so what grew is what a reader without the job log sees."""
    base = repo.commit({"check.py": 'value = row["a"]\n'})
    head = repo.commit({"check.py": "value = type(x) is int\n"})
    merged = subprocess.run(
        (sys.executable, str(SOURCE / "tools/debt_ratchet.py"),
         "--root", repo.root, "--base", base, "--head", head),
        cwd=repo.path, env=ratchet_environment(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=120,
    )
    assert merged.returncode == 1
    assert merged.stdout.rstrip().splitlines()[-1].startswith(("grew ", "found ", "introduced "))


def test_content_cache_digest_covers_a_module_only_loaded_indirectly():
    """The ratchet never imports coordination_tables.attempts by name; agent_comms loads it
    through its own imports. Its source is still one of the ratchet's measuring code, so a
    change to it is a different cache key."""
    indirect = Path(sys.modules["agent_comms.coordination_tables.attempts"].__file__).resolve()
    assert indirect in ContentCache.sources()
    assert (AUDIT_SCRIPTS / "audit" / "findings.py").resolve() in ContentCache.sources()
    assert indirect.is_relative_to(Path(__file__).resolve().parents[2] / "src")


def test_content_cache_prunes_entries_unused_for_the_retention_period(tmp_path, monkeypatch):
    """An entry read after it aged survives the prune; one neither written nor read does not."""
    monkeypatch.setattr(ContentCache, "directory", tmp_path / "debt-ratchet")
    ContentCache.store(1, "read")
    ContentCache.store(2, "unused")
    old = time.time() - (ContentCache.retention_days + 1) * 86400
    for name in ("read", "unused"):
        os.utime(ContentCache.path(name), (old, old))
    assert ContentCache.stored("read") == (1,)
    ContentCache.prune()
    assert ContentCache.stored("read") == (1,)
    assert ContentCache.stored("unused") == ()


def test_a_grown_measure_points_at_every_head_site_it_counts(repo):
    """A summed measure's sites span its files; a per-file measure's stay in its own file."""
    base = repo.commit({"check.py": "value = 1\n", "other.py": "VALUE = 1\n"})
    head = repo.commit({"check.py": "value = 1\nkind = type(x) is int\n",
                        "other.py": "from typing import Any\nVALUE: Any = 1\n"})
    status, report = repo.compare(base, head, TypeIdentity, AnyName)
    assert status == 1
    assert report["found"] == {
        "TypeIdentity": [{"path": f"{repo.root}/check.py", "line": 2}],
        AnyName.at(f"{repo.root}/other.py").text: [{"path": f"{repo.root}/other.py", "line": 2}],
    }


def test_only_the_asked_measures_are_compared_and_located(repo):
    """The audit's raw-shape count has no lines, so it grows without sites, and passes the
    shared runner also took never reach the report."""
    base = repo.commit({"rows.py": "VALUE = 1\n"})
    head = repo.commit({"rows.py": 'def read(row: dict):\n    return row["a"], row["b"], row["c"], row["d"]\n'})
    status, report = repo.compare(base, head, RawRecordShapes)
    assert status == 1
    assert report["delta"] == {RawRecordShapes.at(f"{repo.root}/rows.py").text: 1}
    assert report["found"] == {}
    status, report = repo.compare(base, head, StringSubscript)
    assert report["delta"] == {"StringSubscript": 4}
    assert report["found"] == {"StringSubscript": [{"path": f"{repo.root}/rows.py", "line": 2}] * 4}
