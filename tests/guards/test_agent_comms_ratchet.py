"""agent-comms' ratchet: its declared facts and its CI entry, against real Git commits.

The measures and the comparison are the pinned refactor-audit's and are tested there; these
tests prove what only agent-comms declares (its lock owners, its absence decoder, its codec
modules, its done plan phases and its stored-record probe), that the entry's pool and probes
die with it, and that its command line keeps the report and exit contract."""

import ast
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_comms.child_process import ObservedProcess, SpawnedProcess
from agent_comms.field_codec import FieldCodec
from agent_comms_project import AgentCommsProject
from agent_comms.pinned_audit import audit
from fixture_git import IDENTITY
from process_tree_fixture import TREE, identities, ready

pytestmark = pytest.mark.refactor_guard

SOURCE = Path(__file__).resolve().parents[2]
# This checkout, as the ratchet measures it.
PROJECT = AgentCommsProject(SOURCE)


def search_path(*first: Path) -> str:
    """first, then the inherited PYTHONPATH when the caller's environment has one."""
    inherited = os.environ.get("PYTHONPATH")
    return os.pathsep.join([*map(str, first), *([inherited] if inherited else [])])


def ratchet_environment() -> dict[str, str]:
    """The command's environment: this checkout's src before the inherited path."""
    return {**os.environ, "PYTHONPATH": search_path(SOURCE / "src")}


class Checkout:
    """A fresh Git repository whose files live under one source root, fetching this one's objects."""

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

    def compare(self, base: str, head: str, *measured: type[audit.ratchet.Compared]) -> tuple[int, dict]:
        """The comparison's exit status and report, taking only the named measures'
        measurements. The command line itself is proven by the tests that launch it."""
        result = audit.ratchet.compare(AgentCommsProject(self.path), base, head, self.root, 2, measured)
        return int(result.failed), json.loads(json.dumps(result.report()))


@pytest.fixture(params=["src/agent_comms", "src/toad"])
def repo(tmp_path: Path, request) -> Checkout:
    checkout = tmp_path / "repository"
    checkout.mkdir()
    return Checkout(checkout, request.param)


def test_agent_comms_broad_swallows_stay_within_their_budget():
    root = SOURCE / "src/agent_comms"
    assert sum(audit.source_measures.BroadSwallow.count(PROJECT, path.read_bytes(), str(path)) for path in root.rglob("*.py")) <= 29


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("with store.guarded_read():\n    pass", 0),
        ("async with store.guarded_write_async():\n    pass", 0),
        ("with _store_lock(path, mode, contention), StoreLock.ordered(guards):\n    pass", 1),
        ("fcntl.flock(descriptor, fcntl.LOCK_EX)", 1),
        ("def try_store_lock(descriptor):\n    is_store_lock(path)", 0),
        ('"""with store.guarded_write():"""\nnote = "fcntl.flock("', 0),
        ("ordered(owners)\nlocked = True", 0),
    ],
)
def test_store_lock_calls_count_calls_not_spellings(source, expected):
    assert audit.source_measures.StoreLockCalls.count(PROJECT, source.encode(), "src/agent_comms/example.py") == expected


def test_store_lock_calls_skip_the_store_owners():
    """Locking is store_files' and locked_store's own mechanism, never a hand-placed copy."""
    source = b"with _store_lock(path):\n    pass"
    owners = PROJECT.lock_owners()
    assert all((SOURCE / "src" / owner).is_file() for owner in owners)
    assert [audit.source_measures.StoreLockCalls.count(PROJECT, source, f"src/{owner}") for owner in owners] == [0] * len(owners)


def test_a_new_store_lock_call_fails_on_its_own_file_with_its_line(repo):
    base = repo.commit({"turns.py": "def save(store):\n    with _store_lock(store.path, mode, contention):\n        pass\n",
                        "other.py": "def read(store):\n    with _store_lock(store.path, mode, contention):\n        pass\n"})
    head = repo.commit({"turns.py": "def save(store):\n    pass\n",
                        "other.py": "def read(store):\n    with _store_lock(store.path, mode, contention):\n        pass\n"
                                    "    with _store_lock(store.path, mode, contention):\n        pass\n"})
    status, report = repo.compare(base, head, audit.source_measures.StoreLockCalls)
    assert status == 1
    grown = audit.source_measures.StoreLockCalls.at(f"{repo.root}/other.py").text
    assert report["delta"] == {audit.source_measures.StoreLockCalls.at(f"{repo.root}/turns.py").text: -1, grown: 1}
    assert report["found"] == {grown: [{"path": f"{repo.root}/other.py", "line": line}
                                       for line in (2, 4)]}


def test_compare_timeout_retires_pool_probe_and_defiant_grandchild(tmp_path: Path) -> None:
    repo = Checkout(tmp_path, "src/sample")
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


def test_format_probe_keeps_parent_dependencies_and_reads_archived_source(tmp_path, monkeypatch):
    repo = Checkout(tmp_path, "src/agent_comms")
    source = subprocess.check_output(
        ["git", "archive", "HEAD", "--", "src/agent_comms"], timeout=20
    )
    subprocess.run(["tar", "-x", "-C", str(tmp_path)], input=source, check=True, timeout=60)
    # A Git archive carries no submodule: the complete producer stamp's child reaches the
    # pinned audit only through the measurement's own source paths.
    assert not (tmp_path / audit.project.AUDIT_SCRIPTS.resolve().relative_to(SOURCE) / "audit").exists()
    dependency = tmp_path / "dependencies"
    dependency.mkdir()
    (dependency / "parent_only_dependency.py").write_text("VERSION = 7\n")
    inherited = search_path(dependency)
    monkeypatch.setenv("PYTHONPATH", inherited)
    revision = repo.commit(
        {
            "probe_record.py": (
                "from dataclasses import dataclass\nfrom typing import ClassVar, Literal\n"
                "from parent_only_dependency import VERSION\n"
                "from agent_comms.owner_protocol import CORE_COMMIT\n"
                "@dataclass(frozen=True)\nclass Record:\n"
                "    version: ClassVar[int] = VERSION\n    value: str\n"
                "    commit: Literal[CORE_COMMIT]\n"
            )
        }
    )
    (tmp_path / "src/agent_comms/probe_record.py").write_text(
        "raise RuntimeError('not archived')\n"
    )
    forms = audit.project_measures.UncarriedRecordFormat.formats(
        AgentCommsProject(tmp_path), revision, repo.root, {"src/agent_comms/probe_record.py"}
    )
    assert forms["agent_comms.probe_record.Record"].version == 7
    # The archive holds no Git checkout: the protocol's commit is the revision the stamp
    # factory was given, reached through the complete writer family without asking Git.
    assert forms["agent_comms.probe_record.Record"].fields["commit"]["enum"] == [revision]
    assert os.environ["PYTHONPATH"] == inherited


def test_a_done_phase_fails_the_change_that_breaks_it(tmp_path: Path) -> None:
    """OwnTypeChecksPhase holds at the base, so re-adding an isinstance on our own class
    fails at its line; the phases not done at the base (every merged-commit one, here)
    block nothing."""
    repo = Checkout(tmp_path, "src")
    # The merged-commit phases name this repository's commits; borrowing its objects lets
    # them read "not an ancestor" of the fixture instead of fetching.
    objects = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "--path-format=absolute",
                                       "--git-common-dir"], text=True).strip()
    (tmp_path / ".git/objects/info/alternates").write_text(f"{objects}/objects\n")
    owned = "class Owned:\n    pass\n"
    base = repo.commit({"agent_comms/owned.py": owned})
    head = repo.commit({"agent_comms/owned.py": owned + "\n\ndef owns(value):\n"
                        "    return isinstance(value, Owned)\n"})
    status, report = repo.compare(base, head, audit.project_measures.DonePhaseBroken)
    assert status == 1
    assert report["delta"] == {
        "DonePhaseBroken:OwnTypeChecksPhase::src/agent_comms/owned.py:6": 1,
    }
    clean = repo.commit({"agent_comms/owned.py": owned + "\n\ndef owns(value):\n"
                         "    return value.owned()\n"})
    assert repo.compare(base, clean, audit.project_measures.DonePhaseBroken)[0] == 0


@pytest.mark.parametrize("root", ["src/agent_comms", "src/toad", "src/textual"])
def test_family_flattening_cannot_offset_another_file_or_admit_a_named_facade(
    tmp_path: Path, root: str
):
    repo = Checkout(tmp_path, root)
    original = "def carry(value):\n    return (value.declared_name, value.family_name)\n"
    base = repo.commit({"first.py": original, "second.py": "value = 1\n"})
    head = repo.commit(
        {
            "first.py": "def carry(value):\n    return value\n",
            "second.py": "def carry(value):\n    return value.declared_name\n",
        }
    )
    status, report = repo.compare(base, head, audit.source_measures.FamilyFlattened)
    assert status == 1
    assert report["delta"][audit.source_measures.FamilyFlattened.at(f"{repo.root}/first.py").text] == -2
    assert report["delta"][audit.source_measures.FamilyFlattened.at(f"{repo.root}/second.py").text] == 1
    assert audit.source_measures.FamilyFlattened.count(PROJECT, original.encode(), "consumer.py") == (
        audit.source_measures.FamilyFlattened.occurrences(ast.parse(original))
    )
    original_modules = tuple(
        module for module in PROJECT.mechanism_modules() if module.parent == Path(repo.root)
    )
    facades = {
        f"{directory}/{module.parent.name}/{module.name}"
        for module in PROJECT.mechanism_modules()
        for directory in ("nested", "vendor")
    }
    facades.update(f"nested/{module.name}" for module in PROJECT.mechanism_modules())
    moved = repo.commit(
        {
            "second.py": "def carry(value):\n    return value\n",
            "another_codec.py": original,
            **{module.name: original for module in original_modules},
            **{path: original for path in facades},
        }
    )
    status, report = repo.compare(head, moved, audit.source_measures.FamilyFlattened)
    assert status == 1
    assert report["delta"][audit.source_measures.FamilyFlattened.at(f"{repo.root}/another_codec.py").text] == 2
    for module in original_modules:
        assert report["head"][audit.source_measures.FamilyFlattened.at(module.as_posix()).text] == 0
    for path in facades:
        assert report["head"][audit.source_measures.FamilyFlattened.at(f"{repo.root}/{path}").text] == 2


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
    status, report = repo.compare(base, head, audit.source_measures.NoneAbsence)
    assert status == 1
    assert {identity: lines for identity, lines in report["introduced"].items() if lines} == {
        audit.source_measures.NoneAbsence.at(f"{repo.root}/owner.py").text: [1],
        audit.source_measures.NoneAbsence.at(f"{repo.root}/foreign.py").text: [5],
    }
    plan = audit.source_measures.NoneAbsence.at(f"{repo.root}/plan.py").text
    assert report["introduced"][plan] == [] and report["delta"][plan] == 0


def test_growth_lines_end_the_merged_log(repo: Checkout) -> None:
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
    assert indirect in PROJECT.measuring_sources()
    assert (audit.project.AUDIT_SCRIPTS / "audit" / "findings.py").resolve() in PROJECT.measuring_sources()
    assert indirect.is_relative_to(Path(__file__).resolve().parents[2] / "src")
