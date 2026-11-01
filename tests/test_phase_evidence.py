import ast
import gc
import subprocess
import weakref
from pathlib import Path

import pytest

from agent_comms.head_review import HeadReview, MergeHeadReview
from agent_comms.phase_evidence import (
    AllEvidence,
    ClassDeclared,
    ClassFlagsAbsent,
    ClassChecks,
    ClassRostersAbsent,
    CommitMerged,
    FunctionDeclared,
    MainEvidence,
    MainTree,
    ModuleBindings,
    QualifiedName,
    NoAbsenceProbes,
    NoExactCopies,
    NoGodFunction,
    OwnClassChecksAbsent,
    ParentsShared,
    PathsAbsent,
    DocumentPatternPresent,
    PatternAbsent,
    PatternPresent,
    PhaseEvidence,
    PlanReading,
    RestatedMembersAbsent,
    RulesetGates,
    UnreadableEvidenceError,
)
from nominal_refactor_advisor.audit.findings import GodFunction

from agent_comms.nominal_census import NominalCensus
from agent_comms.plan_phases import BusDeletionPhase, FLAG, ExternalBoundariesPhase, PlanPhase, RostersPhase
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.work_branches import WorkBranches
from fixture_git import IDENTITY


def git(checkout, *args):
    return subprocess.run(("git", *IDENTITY, "-C", str(checkout), *args), check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def test_evidence_reads_origin_main_and_never_the_worktree(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/kept.py").write_text("flag = True\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "main")
    sha = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", sha)
    (tmp_path / "src/kept.py").unlink()
    reading = PlanReading((), MainTree.of(
        Repo(checkout=tmp_path, github=RepositoryName.from_wire("OpenHCSDev/agent-comms")), NominalCensus()))

    assert reading.holds(CommitMerged(sha))
    assert reading.holds(PathsAbsent(("src/gone.py",)))
    assert not reading.holds(PathsAbsent(("src/kept.py",)))
    assert reading.holds(PatternPresent("flag = True"))
    assert not reading.holds(PatternAbsent("flag = True"))
    assert reading.holds(PatternAbsent("flag = True", ("src", ":!src/kept.py")))
    assert not reading.holds(AllEvidence((CommitMerged(sha), PathsAbsent(("src/kept.py",)))))


def test_boundary_evidence_ignores_its_declaration_and_exception_groups(tmp_path):
    git(tmp_path, "init", "-q")
    source = tmp_path / "src" / "boundary.py"
    source.parent.mkdir()
    source.write_text(
        f"evidence = {ExternalBoundariesPhase.evidence!r}\n"
        "try:\n    pass\nexcept ExceptionGroup:\n    raise\n"
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "boundary")
    sha = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", sha)
    reading = PlanReading((), MainTree.of(
        Repo(checkout=tmp_path, github=RepositoryName.from_wire("OpenHCSDev/agent-comms")), NominalCensus()))
    assert reading.holds(ExternalBoundariesPhase.evidence)

    source.write_text("try:\n    pass\nexcept Exception:\n    raise\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "broad")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
    changed = PlanReading((), MainTree.of(reading.main.repo, NominalCensus()))
    assert not changed.holds(ExternalBoundariesPhase.evidence)


def test_every_plan_phase_declares_its_plan_and_evidence():
    phases = PlanPhase.members_with(PlanPhase)
    assert phases and all(phase.plan.document.startswith("plans/") for phase in phases)
    assert all(isinstance(phase.phase_evidence(), PhaseEvidence) for phase in phases)


def main_of(tmp_path, files):
    git(tmp_path, "init", "-q")
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "main")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
    return PlanReading((), MainTree.of(
        Repo(checkout=tmp_path, github=RepositoryName.from_wire("OpenHCSDev/agent-comms")), NominalCensus()))


def test_roster_evidence_keeps_external_abi_and_each_remaining_internal_registry(tmp_path):
    source = Path(__file__).parents[1] / "src/agent_comms"
    reading = main_of(tmp_path, {
        "src/phase_evidence.py": (source / "phase_evidence.py").read_text(),
        "src/plan_phases.py": (source / "plan_phases.py").read_text(),
        "src/producer.py": "PI_0_85_1_EVENT_KINDS = frozenset({'alpha', 'beta'})\n"
                           "class Runtime:\n    event_kinds = PI_0_85_1_EVENT_KINDS\n",
        "src/unrelated.py": "class OtherOwner:\n    members = {}\n"
                            "def local():\n    types = {}\n",
    })
    assert reading.holds(RostersPhase.evidence)
    for path, remaining in (
        ("src/remaining.py", "THREAD_CAPABILITIES: tuple[type, ...] = (Steers, Compacts)\n"),
        ("src/remaining.py", "class Backend:\n    serves_threads = True\n"),
        ("src/remaining.py", "class Backend:\n    serves_new_threads: ClassVar[bool] = True\n"),
        ("src/agent_comms/installer/probes/carry.py", "class Carry:\n    declared: ClassVar[dict] = {}\n"),
        ("src/agent_comms/private_registry_guard.py", "class RegistryPrivacy:\n    kinds = {}\n"),
        ("src/agent_comms/thread_identity.py", "class BirthlessIncarnation:\n    members = {}\n"),
        ("src/agent_comms/tool_calls.py", "class CliToolArguments:\n    types = {}\n"),
        ("src/agent_comms/pi_events.py", "class PiEvent:\n    _start_phases = {}\n"),
        ("src/agent_comms/pi_events.py", "class PiEvent:\n    _end_events = {}\n"),
    ):
        remaining_file = tmp_path / path
        remaining_file.parent.mkdir(parents=True, exist_ok=True)
        remaining_file.write_text(remaining)
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "registry")
        git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
        changed = PlanReading((), MainTree.of(reading.main.repo, NominalCensus()))
        assert not changed.holds(RostersPhase.evidence), remaining
        remaining_file.unlink()

    (tmp_path / "src/remaining.py").write_text("class Carry:\n    declared = {}\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "unrelated same-named class")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
    changed = PlanReading((), MainTree.of(reading.main.repo, NominalCensus()))
    assert changed.holds(RostersPhase.evidence)
    (tmp_path / "src/remaining.py").unlink()


def test_main_tree_grep_uses_the_same_repository_tree_as_its_module_reader(tmp_path):
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    main_of(dependency, {"src/debt.py": "class Carry:\n    declared = {}\n"})
    checkout = tmp_path / "owner"
    checkout.mkdir()
    reading = main_of(checkout, {"src/owner.py": "class OtherOwner:\n    members = {}\n"})
    git(checkout, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(dependency), "src/dependency")
    git(checkout, "commit", "-qam", "dependency")
    git(checkout, "update-ref", "refs/remotes/origin/main", git(checkout, "rev-parse", "HEAD"))
    git(checkout, "config", "submodule.recurse", "true")
    assert "src/dependency" in git(checkout, "grep", "--recurse-submodules", "-n",
                                   "-e", "declared", "HEAD", "--", "src")
    current = PlanReading((), MainTree.of(reading.main.repo, NominalCensus()))
    assert current.holds(RostersPhase.evidence)
    assert not current.main.matching_lines(r"declared", ("src",))
    assert len(tuple(current.main.modules(("src",)))) == 1


@pytest.mark.parametrize("paths", [
    ("src",), ("src/kept.py",), ("src/nested",), ("src", "src2"), ("src2/kept.py", "src/kept.py"),
    ("src/nested.py",), ("gone",),
])
def test_modules_answer_the_listing_git_gives_for_their_paths(tmp_path, paths):
    """A directory named like a module, and a sibling sharing a path's prefix, are not modules."""
    main = main_of(tmp_path, {
        "src/kept.py": "kept = 1\n", "src/nested/inner.py": "inner = 1\n",
        "src/nested.py/held.py": "held = 1\n", "src/notes.md": "notes\n", "src2/kept.py": "other = 1\n",
    }).main
    listed = git(tmp_path, "ls-tree", "-r", "--name-only", main.commit, "--", *paths).splitlines()
    assert [module.file_path for module in main.modules(paths)] == [
        name for name in listed if name.endswith(".py")]


def test_named_declarations_parse_only_their_paths_and_stop_after_a_match(tmp_path: Path) -> None:
    main = main_of(tmp_path, {
        "src/owner.py": "class Owner:\n    pass\n",
        "src/z_unrelated.py": "not valid Python !\n",
        "tests/first.py": "def wanted():\n    pass\n",
        "tests/z_unneeded.py": "not valid Python !\n",
    }).main
    assert ClassDeclared("Owner", ("src/owner.py",)).read(main)
    assert {path for _, path in main.bindings} == {"src/owner.py"}
    assert FunctionDeclared("wanted", ("tests",)).read(main)
    assert {path for roots, path in main.bindings if roots == ("tests",)} == {"tests/first.py"}


def test_following_reading_reuses_only_unchanged_blob_facts(tmp_path: Path) -> None:
    reading = main_of(tmp_path, {
        "src/kept.py": "class Kept:\n    pass\n",
        "src/changed.py": "class Before:\n    pass\n",
        "src/deleted.py": "class Deleted:\n    pass\n",
    })
    assert not ClassDeclared("Missing").read(reading.main)
    held = dict(reading.main.bindings)
    (tmp_path / "src/changed.py").write_text("class After:\n    pass\n")
    (tmp_path / "src/deleted.py").unlink()
    git(tmp_path, "add", "src")
    git(tmp_path, "commit", "-qm", "replace and delete declarations")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
    following = reading.following((), MainTree.of(reading.main.repo, NominalCensus()))
    assert following.main.bindings is not reading.main.bindings
    assert reading.main.bindings == held
    assert following.main.bindings == {(("src",), "src/kept.py"): held[("src",), "src/kept.py"]}
    assert following.main.bindings[("src",), "src/kept.py"][1] is held[("src",), "src/kept.py"][1]
    assert not ClassDeclared("Before").read(following.main)
    assert not ClassDeclared("Deleted").read(following.main)
    assert ClassDeclared("After").read(following.main)
    assert ClassDeclared("Before").read(reading.main)


def test_source_checks_release_their_method_and_comparison_nodes() -> None:
    module = ast.parse("class Value:\n    def __eq__(self, other):\n"
                       "        if not isinstance(other, Value):\n"
                       "            return NotImplemented\n"
                       "        return self.key == other.key\n")
    methods = [weakref.ref(node) for node in ast.walk(module)
               if isinstance(node, (ast.FunctionDef, ast.Call))]
    checks = ClassChecks()
    checks.visit(module)
    assert not checks.owned()
    del module
    gc.collect()
    assert all(method() is None for method in methods)


def test_module_binding_facts_do_not_retain_the_source_nodes() -> None:
    module = ast.parse("class Parent:\n    flag = True\n"
                       "    def method(self):\n        return 1\n"
                       "class Child(Parent):\n    values = (Parent, *OTHERS)\n")
    syntax = [weakref.ref(node) for node in ast.walk(module)
              if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.Assign))]
    bindings = ModuleBindings("src/owner.py", QualifiedName.of("owner"), QualifiedName(()))
    bindings.visit(module)
    assert bindings.classes
    del module
    gc.collect()
    assert all(node() is None for node in syntax)


def test_changed_paths_between_commits_and_of_a_commit_against_itself(tmp_path):
    first = main_of(tmp_path, {"src/kept.py": "kept = 1\n"}).main.commit
    (tmp_path / "src/kept.py").write_text("kept = 2\n")
    git(tmp_path, "commit", "-qam", "next")
    second = git(tmp_path, "rev-parse", "HEAD")
    branches = WorkBranches(tmp_path)
    assert branches.changed(first, second) == {"src/kept.py"}
    assert branches.changed(second, second) == frozenset()


def test_bus_deletion_evidence_reads_uses_without_matching_its_own_declaration(tmp_path):
    reading = main_of(tmp_path, {
        "src/phase.py": "from agent_comms.phase_evidence import BusStorageAbsent\n"
                        "evidence = BusStorageAbsent()\n"
                        "# Delete bus.jsonl and bus_meta once the carry is accepted.\n"
                        "def documentation():\n    'bus.jsonl'\n",
    })
    assert reading.holds(BusDeletionPhase.evidence)
    # Every production spelling found during the owner trace must make the phase fail.
    for source in (
        "class WireLog:\n    pass\n",
        "def has_private_wire_fields(value):\n    return False\n",
        "from somewhere.wire_log import WireLog\n",
        "from somewhere.wire_log import another_name\n",
        "def use(owner):\n    return owner._wire_lock_path\n",
        "def use(owner):\n    return owner / 'wire'\n",
        "def use(owner):\n    return owner.with_name('wire')\n",
        "def use(owner):\n    return owner.joinpath('wire')\n",
        "def use(owner):\n    return owner / 'bus.jsonl'\n",
        "def use(owner):\n    return owner / 'bus_meta.json'\n",
    ):
        (tmp_path / "src/uses.py").write_text(source)
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "use")
        git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
        changed = PlanReading((), MainTree.of(reading.main.repo, NominalCensus()))
        assert not changed.holds(BusDeletionPhase.evidence), source


def test_source_shape_evidence_reads_declarations_on_main(tmp_path):
    copy = "    def __init__(self, path):\n        self.path = path\n"
    reading = main_of(tmp_path, {
        "src/a.py": "class Store:\n" + copy + "\nclass Child(Store):\n    pass\n",
        "src/b.py": "class Other:\n" + copy.replace("path", "root") + "\n"
                    "def long():\n" + "    x = 1\n" * GodFunction.threshold() + "\n"
                    "def probe(other):\n    return other.value is None\n"
                    "def check(value):\n    return isinstance(value, (int, Store))\n",
        "src/caps.py": "class Shared:\n    pass\n\nclass Lonely:\n    pass\n",
        "src/members.py": "from caps import Shared, Lonely\n"
                          "class One(Shared):\n    pass\nclass Two(Shared, Lonely):\n    pass\n",
    })

    assert not reading.holds(NoExactCopies(("__init__",), ("src/a.py", "src/b.py")))
    assert reading.holds(NoExactCopies(("__init__",), ("src/a.py",)))
    assert not reading.holds(NoGodFunction("src/b.py", "long"))
    assert reading.holds(NoGodFunction("src/b.py", "check"))
    assert not reading.holds(NoAbsenceProbes(("src/b.py",)))
    assert reading.holds(NoAbsenceProbes(("src/a.py",)))
    assert not reading.holds(OwnClassChecksAbsent())
    assert not reading.holds(ParentsShared(paths=("src/caps.py",)))
    assert reading.holds(ParentsShared(names=("Shared",)))
    assert not reading.holds(ParentsShared(names=("Missing",)))


@pytest.mark.parametrize(("source", "allowed"), (
    ("class Value:\n"
     "    def __eq__(self, other):\n"
     "        if not isinstance(other, Value):\n"
     "            return NotImplemented\n"
     "        return self.key == other.key\n", True),
    ("class Value:\n"
     "    def __lt__(self, other):\n"
     "        if not isinstance(other, Value):\n"
     "            return NotImplemented\n"
     "        return isinstance(self.key, Value)\n", False),
    ("class Value:\n"
     "    def __eq__(self, other):\n"
     "        if not isinstance(other, Value):\n"
     "            return False\n", False),
    ("NotImplemented = False\n"
     "class Value:\n"
     "    def __eq__(self, other):\n"
     "        if not isinstance(other, Value):\n"
     "            return NotImplemented\n", False),
    ("class Value:\n"
     "    def __eq__(self, other):\n"
     "        NotImplemented = False\n"
     "        if not isinstance(other, Value):\n"
     "            return NotImplemented\n", False),
    ("class Value:\n"
     "    def __eq__(self, other):\n"
     "        if not isinstance(self.key, Value):\n"
     "            return NotImplemented\n", False),
    ("class Value: pass\n"
     "def __eq__(self, other):\n"
     "    if not isinstance(other, Value):\n"
     "        return NotImplemented\n", False),
    ("class Value:\n"
     "    def __hash__(self):\n"
     "        if not isinstance(self, Value):\n"
     "            return NotImplemented\n", False),
))
def test_only_foreign_comparison_refusal_exempts_an_owned_type_check(source, allowed):
    checks = ClassChecks()
    checks.visit(ast.parse(source))
    assert (not checks.owned()) is allowed


@pytest.mark.parametrize(("required", "queue", "actors", "expected"), (
    (MergeHeadReview.required_contexts(), True, [], True),
    (MergeHeadReview.required_contexts(), False, [], False),
    (MergeHeadReview.required_contexts(), True, [{"actor_type": "RepositoryRole", "actor_id": 5}], False),
    (MergeHeadReview.required_contexts(), True, [{"actor_type": "Integration", "actor_id": 15368}], False),
    ((HeadReview.context,), True, [], False),
    ((MergeHeadReview.context,), True, [], False),
    (("review-verdict",), True, [], False),
    (("smoke",), True, [], False),
))
def test_ruleset_requires_both_owned_contexts_and_actual_queue_without_bypass(
    tmp_path, required, queue, actors, expected,
):
    main = main_of(tmp_path, {"src/a.py": "x = 1\n"}).main
    # The real API may split independent requirements across rulesets.
    rules = [{"ruleset": 7, "required": ["smoke"], "queue": False},
             *({"ruleset": 8, "required": [context], "queue": False} for context in required),
             {"ruleset": 9, "required": [], "queue": queue}]
    answers = {"rules/branches/main": rules, "rulesets/7": [],
               "rulesets/8": actors, "rulesets/9": []}
    reads = []

    class Answered(RulesetGates):
        @staticmethod
        def api(main, path, query):
            reads.append(path)
            return answers[path]

    assert Answered().holds(PlanReading((), main)) is expected
    assert reads.count("rulesets/8") == 1
    assert all(context in RulesetGates().describe() for context in MergeHeadReview.required_contexts())


def test_cleanup_evidence_counts_restated_members_and_class_rosters(tmp_path):
    reading = main_of(tmp_path, {
        "src/base.py": "from dataclasses import dataclass\n"
                       "class Base:\n    kind = 'base'\n    __slots__ = ()\n"
                       "    def answer(self):\n        return 1\n"
                       "@dataclass\nclass Record:\n    items: tuple = ()\n"
                       "class Plain:\n    items: tuple = ()\n",
        "src/other.py": "class Base:\n    kind = 'base'\n",
        "src/child.py": "from dataclasses import dataclass\nimport base\n"
                        "from other import Base as Copy\n"
                        "class Child(base.Base):\n    kind = 'base'\n    __slots__ = ()\n"
                        "    def answer(self):\n        return 1\n"
                        "class Other(base.Base):\n    kind = 'other'\n"
                        "    def answer(self):\n        return 2\n"
                        "@dataclass\nclass Narrow(base.Record):\n    items: tuple = ()\n"
                        "@dataclass\nclass Field(base.Plain):\n    items: tuple = ()\n"
                        "class Twin(Copy):\n    kind = 'base'\n"
                        "ROSTER = (Child, Other)\nMIXED = (Child, int)\n"
                        "ERRORS = (OSError, base.Base, Other)\nMORE = (int, *ROSTER)\n"
                        "class Phase:\n    after = (Child, Other)\n    rows = (Child, Narrow)\n",
    })
    restated = RestatedMembersAbsent()
    rosters = ClassRostersAbsent(families=(PlanPhase,))

    assert list(restated.restatements(reading.main)) == [
        "src/child.py:5 Child.kind", "src/child.py:7 Child.answer",
        "src/child.py:15 Narrow.items", "src/child.py:20 Twin.kind",
    ]
    assert list(rosters.rosters(reading.main)) == [
        "src/child.py:21 ROSTER", "src/child.py:23 ERRORS", "src/child.py:24 MORE",
        "src/child.py:26 Phase.after", "src/child.py:27 Phase.rows",
    ]
    assert not reading.holds(restated)
    assert not reading.holds(rosters)


def test_roster_relation_is_exempt_only_below_the_family_by_qualified_name(tmp_path):
    reading = main_of(tmp_path, {
        "src/agent_comms/plan_phases.py": "class PlanPhase:\n    pass\n",
        "src/decoy.py": "class PlanPhase:\n    pass\n",
        "src/phases.py": "from agent_comms.plan_phases import PlanPhase\n"
                         "from decoy import PlanPhase as Spoofed\n"
                         "class A:\n    pass\nclass B:\n    pass\n"
                         "class Real(PlanPhase):\n    after = (A, B)\n"
                         "class Deeper(Real):\n    after = (A, B)\n"
                         "class Unrelated:\n    after = (A, B)\n"
                         "class Spoof(Spoofed):\n    after = (A, B)\n",
    })

    assert list(ClassRostersAbsent(families=(PlanPhase,)).rosters(reading.main)) == [
        "src/phases.py:12 Unrelated.after", "src/phases.py:14 Spoof.after",
    ]


def test_declared_evidence_reads_declarations_not_their_spelling(tmp_path):
    main = main_of(tmp_path, {
        "src/agent_comms/tools.py": "TEXT = 'class Ghost(Base):'\n"
                                    "# class Phantom(Base):\n"
                                    "class Real:\n    def compact(self):\n"
                                    "        def inner():\n            pass\n",
        "src/other.py": "class Elsewhere:\n    pass\n",
        "tests/test_x.py": "def test_it():\n    pass\n",
    }).main

    assert ClassDeclared("Real", ("src/agent_comms",)).read(main)
    assert not ClassDeclared("Ghost").read(main)
    assert not ClassDeclared("Phantom").read(main)
    assert not ClassDeclared("Elsewhere", ("src/agent_comms",)).read(main)
    assert ClassDeclared("Elsewhere").read(main)
    assert FunctionDeclared("compact", ("src/agent_comms/tools.py",)).read(main)
    assert not FunctionDeclared("inner").read(main)
    assert not FunctionDeclared("Real").read(main)
    assert FunctionDeclared("test_it", ("tests",)).read(main)


def test_lifecycle_flags_count_class_bodies_not_locals(tmp_path):
    local = "def stage():\n    complete = False\n    complete = True\n"
    reading = main_of(tmp_path, {"src/local.py": local})
    assert reading.holds(ClassFlagsAbsent(r"^\s+(complete|active)" + FLAG))

    flagged = main_of(tmp_path, {"src/flag.py": "class Status:\n    active: ClassVar[bool] = False\n"
                                                 "class Scope:\n    complete: bool = True\n"})
    assert not flagged.holds(ClassFlagsAbsent(r"^\s+(complete|active)" + FLAG))
    assert flagged.holds(ClassFlagsAbsent(r"^\s+(complete)" + FLAG))

    # A transcript quoting a flag is prose, not a module: never parsed, never counted.
    quoted = main_of(tmp_path, {"src/log.md": "I’ll check it.\n    complete = True\n"})
    assert quoted.holds(ClassFlagsAbsent(r"^\s+(complete)" + FLAG))


def test_main_evidence_reads_once_per_commit_and_unknown_is_reported(tmp_path):
    reading = main_of(tmp_path, {"src/a.py": "x = 1\n"})
    reads = []

    class Counted(PatternPresent):
        def read(self, main):
            reads.append(main.commit)
            return super().read(main)

    counted = Counted("x = 1")
    assert reading.holds(counted)
    again = reading.following((), reading.main)
    assert again.holds(counted)
    assert len(reads) == 1

    class Unreadable(PatternPresent):
        def read(self, main):
            raise UnreadableEvidenceError("GitHub is down")

    unreadable = Unreadable("x")
    assert not reading.holds(unreadable)
    assert reading.describe(unreadable).startswith("unknown (GitHub is down)")
    assert unreadable not in reading.following((), reading.main).reads


def test_document_landing_evidence_reads_main_without_counting_quoted_source(tmp_path):
    document = "plans/OWNER-ASKS-PLAN.md"
    landing = "- page_quarter_scroll landed: OpenHCSDev/toad@" + "a" * 40
    reading = main_of(tmp_path, {document: landing + "\n", "src/example.md": "guarded_read()\n"})
    evidence = DocumentPatternPresent(r"^- page_quarter_scroll landed: OpenHCSDev/toad@[0-9a-f]{40}$", (document,))
    (tmp_path / document).write_text("changed worktree\n")
    assert reading.holds(evidence)
    assert reading.holds(PatternAbsent(r"guarded_read\("))
    assert not reading.holds(DocumentPatternPresent("missing landing", (document,)))
    git(tmp_path, "add", document)
    git(tmp_path, "commit", "-qm", "remove landing")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
    assert not PlanReading((), MainTree.of(reading.main.repo, NominalCensus())).holds(evidence)
