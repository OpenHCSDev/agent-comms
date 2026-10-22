import subprocess

import pytest

from agent_comms.phase_evidence import (
    AllEvidence,
    ClassFlagsAbsent,
    CommitMerged,
    EvidenceUnknown,
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
    PlanReading,
    RulesetGates,
)
from agent_comms.head_review import HeadReview
from agent_comms.pinned_audit import GodFunction
from agent_comms.plan_phases import ExternalBoundariesPhase, PlanPhase, RemainingSlopPhase
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


def test_every_plan_phase_declares_its_plan():
    phases = PlanPhase.members_with(PlanPhase)
    assert phases and all(phase.plan.document.startswith("plans/") for phase in phases)


def test_a_phase_without_evidence_cannot_be_declared():
    with pytest.raises(TypeError, match="declares no evidence"):
        class UnprovablePhase(RemainingSlopPhase):
            pass


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


def test_ruleset_gates_only_without_an_always_bypass():
    ruleset = {
        "rules": [{"type": "required_status_checks",
                   "parameters": {"required_status_checks": [{"context": "review-verdict"}]}}],
        "bypass_actors": [{"actor_type": "RepositoryRole", "bypass_mode": "pull_request"}],
    }
    gates = RulesetGates(HeadReview)
    assert gates.gates(ruleset)
    assert HeadReview.context in gates.describe()
    ruleset["bypass_actors"][0]["bypass_mode"] = "always"
    assert not gates.gates(ruleset)
    ruleset["rules"][0]["parameters"]["required_status_checks"] = [{"context": "smoke"}]
    ruleset["bypass_actors"] = []
    assert not gates.gates(ruleset)


def test_lifecycle_flags_count_class_bodies_not_locals(tmp_path):
    local = "def stage():\n    complete = False\n    complete = True\n"
    reading = main_of(tmp_path, {"src/local.py": local})
    assert reading.holds(ClassFlagsAbsent(("complete", "active")))

    flagged = main_of(tmp_path, {"src/flag.py": "class Status:\n    active: ClassVar[bool] = False\n"
                                                 "class Scope:\n    complete: bool = True\n"})
    assert not flagged.holds(ClassFlagsAbsent(("complete", "active")))
    assert flagged.holds(ClassFlagsAbsent(("complete",)))


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
            raise EvidenceUnknown("GitHub is down")

    unreadable = Unreadable("x")
    assert not reading.holds(unreadable)
    assert reading.describe(unreadable).startswith("unknown (GitHub is down)")
    assert (reading.main, unreadable) not in MainEvidence.results
