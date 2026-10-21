import subprocess

from agent_comms.phase_evidence import (
    AllEvidence,
    CommitMerged,
    MainTree,
    NoDeclaredEvidence,
    PathsAbsent,
    PatternAbsent,
    PatternPresent,
    PlanReading,
)
from agent_comms.plan_phases import ExternalBoundariesPhase, PlanPhase
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
    assert not reading.holds(AllEvidence((CommitMerged(sha), NoDeclaredEvidence())))


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
