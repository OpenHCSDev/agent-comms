import subprocess
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.head_review import HeadReview
from agent_comms.phase_evidence import (
    AllEvidence,
    AlwaysBypass,
    BranchRule,
    BypassMode,
    ClassFlagsAbsent,
    CommitMerged,
    MainEvidence,
    MainTree,
    NoAbsenceProbes,
    NoExactCopies,
    NoGodFunction,
    OwnClassChecksAbsent,
    ParentsShared,
    PathsAbsent,
    PatternAbsent,
    PatternPresent,
    PhaseEvidence,
    PlanReading,
    PullRequestBypass,
    RulesetGates,
    UnlistedBypass,
    UnreadableEvidenceError,
)
from agent_comms.pinned_audit import GodFunction
from agent_comms.plan_phases import FLAG, ExternalBoundariesPhase, PlanPhase, RostersPhase
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName


def git(checkout, *args):
    return subprocess.run(("git", "-C", str(checkout), *args), check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def test_evidence_reads_origin_main_and_never_the_worktree(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/kept.py").write_text("flag = True\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main")
    sha = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", sha)
    (tmp_path / "src/kept.py").unlink()
    reading = PlanReading((), MainTree.of(
        Repo(checkout=tmp_path, github=RepositoryName.from_wire("OpenHCSDev/agent-comms"))))

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
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "boundary")
    sha = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", sha)
    reading = PlanReading((), MainTree.of(
        Repo(checkout=tmp_path, github=RepositoryName.from_wire("OpenHCSDev/agent-comms"))))
    assert reading.holds(ExternalBoundariesPhase.evidence)

    source.write_text("try:\n    pass\nexcept Exception:\n    raise\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "broad")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
    changed = PlanReading((), MainTree.of(reading.main.repo))
    assert not changed.holds(ExternalBoundariesPhase.evidence)


def test_every_plan_phase_declares_its_plan_and_evidence():
    phases = PlanPhase.members_with(PlanPhase)
    assert phases and all(phase.plan.document.startswith("plans/") for phase in phases)
    assert all(isinstance(phase.evidence, PhaseEvidence) for phase in phases)


def main_of(tmp_path, files):
    git(tmp_path, "init", "-q")
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
    return PlanReading((), MainTree.of(
        Repo(checkout=tmp_path, github=RepositoryName.from_wire("OpenHCSDev/agent-comms"))))


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
    for remaining in (
        "THREAD_CAPABILITIES: tuple[type, ...] = (Steers, Compacts)\n",
        "class Backend:\n    serves_threads = True\n",
        "class Backend:\n    serves_new_threads: ClassVar[bool] = True\n",
        "class Carry:\n    declared: ClassVar[dict] = {}\n",
        "class ThreadRename:\n    kinds = {}\n",
        "class BirthlessIncarnation:\n    members = {}\n",
        "class CliToolArguments:\n    types = {}\n",
        "class PiTurnPhase:\n    _start_phases = {}\n",
        "class PiTurnPhase:\n    _end_events = {}\n",
    ):
        (tmp_path / "src/remaining.py").write_text(remaining)
        git(tmp_path, "add", ".")
        git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "registry")
        git(tmp_path, "update-ref", "refs/remotes/origin/main", git(tmp_path, "rev-parse", "HEAD"))
        changed = PlanReading((), MainTree.of(reading.main.repo))
        assert not changed.holds(RostersPhase.evidence), remaining


def test_main_tree_grep_uses_the_same_repository_tree_as_its_module_reader(tmp_path):
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    main_of(dependency, {"src/debt.py": "class Carry:\n    declared = {}\n"})
    checkout = tmp_path / "owner"
    checkout.mkdir()
    reading = main_of(checkout, {"src/owner.py": "class OtherOwner:\n    members = {}\n"})
    git(checkout, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(dependency), "src/dependency")
    git(checkout, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "dependency")
    git(checkout, "update-ref", "refs/remotes/origin/main", git(checkout, "rev-parse", "HEAD"))
    git(checkout, "config", "submodule.recurse", "true")
    assert "src/dependency" in git(checkout, "grep", "--recurse-submodules", "-n",
                                   "-e", "declared", "HEAD", "--", "src")
    current = PlanReading((), MainTree.of(reading.main.repo))
    assert current.holds(RostersPhase.evidence)
    assert not current.main.matching_lines(r"declared", ("src",))
    assert len(tuple(current.main.modules(("src",)))) == 1


def test_source_shape_evidence_reads_declarations_on_main(tmp_path):
    copy = "    def __init__(self, path):\n        self.path = path\n"
    reading = main_of(tmp_path, {
        "src/a.py": "class Store:\n" + copy + "\nclass Child(Store):\n    pass\n",
        "src/b.py": "class Other:\n" + copy.replace("path", "root") + "\n"
                    "def long():\n" + "    x = 1\n" * GodFunction.threshold + "\n"
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


def test_ruleset_gates_only_while_no_actor_skips_the_review(tmp_path):
    modes = FieldCodec.decode_external(
        tuple[type[BypassMode], ...], ["pull_request", "always", "exempt"], {})
    assert modes == (PullRequestBypass, AlwaysBypass, UnlistedBypass)
    assert [mode.keeps_gate() for mode in modes] == [True, False, False]

    main = main_of(tmp_path, {"src/a.py": "x = 1\n"}).main
    rules = [{"ruleset": 7, "required": ["smoke"]},
             {"ruleset": 8, "required": [HeadReview.queue_check]}]
    assert [rule.requires(HeadReview.queue_check)
            for rule in FieldCodec.decode(tuple[BranchRule, ...], rules)] == [False, True]

    def gates(bypass):
        answers = {"rules/branches/main": rules, "rulesets/8": bypass}

        class Answered(RulesetGates):
            @staticmethod
            def api(main, path, query):
                return answers[path]

        return Answered().holds(PlanReading((), main))

    assert gates(["pull_request"])
    assert gates([])
    assert not gates(["pull_request", "always"])
    assert HeadReview.context in RulesetGates().describe()


def test_lifecycle_flags_count_class_bodies_not_locals(tmp_path):
    local = "def stage():\n    complete = False\n    complete = True\n"
    reading = main_of(tmp_path, {"src/local.py": local})
    assert reading.holds(ClassFlagsAbsent(r"^\s+(complete|active)" + FLAG))

    flagged = main_of(tmp_path, {"src/flag.py": "class Status:\n    active: ClassVar[bool] = False\n"
                                                 "class Scope:\n    complete: bool = True\n"})
    assert not flagged.holds(ClassFlagsAbsent(r"^\s+(complete|active)" + FLAG))
    assert flagged.holds(ClassFlagsAbsent(r"^\s+(complete)" + FLAG))


def test_main_evidence_reads_once_per_commit_and_unknown_is_reported(tmp_path):
    reading = main_of(tmp_path, {"src/a.py": "x = 1\n"})
    reads = []

    class Counted(PatternPresent):
        def read(self, main):
            reads.append(main.commit)
            return super().read(main)

    counted = Counted("x = 1")
    again = PlanReading((), reading.main)
    assert reading.holds(counted) and again.holds(counted)
    assert len(reads) == 1

    class Unreadable(PatternPresent):
        def read(self, main):
            raise UnreadableEvidenceError("GitHub is down")

    unreadable = Unreadable("x")
    assert not reading.holds(unreadable)
    assert reading.describe(unreadable).startswith("unknown (GitHub is down)")
    assert (reading.main, unreadable) not in MainEvidence.results
