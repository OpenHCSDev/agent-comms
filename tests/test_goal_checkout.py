"""Goal authority and Git's declared default drive the real checkout command."""

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread
from cli_command import core
from source_revision_fixture import SourceRepository


def test_goal_checkout_uses_origin_default_and_retires(
    private_comms, tmp_path, monkeypatch,
):
    comms = private_comms
    repository = SourceRepository.create(
        tmp_path / "source", RepositoryName("OpenHCSDev", "OpenMFD"),
    )
    repository.git(repository.checkout, "branch", "-m", "main", "master")
    repository.git(repository.checkout, "push", "origin", "master")
    repository.git(repository.remote, "symbolic-ref", "HEAD", "refs/heads/master")
    repository.git(repository.checkout, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    rule = StorageRule(checkout_base=tmp_path / "checkouts",
                       scratch_base=tmp_path / "scratch", floor_gib=0)
    StorageRule.path_in(comms.root).write_text(FieldCodec.compact(FieldCodec.encode(rule)))
    worker = comms.registry.declare(Thread(
        "worker", frozenset(), str(repository.checkout), execution=ExternalThreadExecution,
    ))
    goal = comms.goals.update_goal(worker.name, SetGoalAction(text="Work in OpenMFD"))
    monkeypatch.setenv("AGENT_COMMS_THREAD", worker.name)
    command = ("--root", str(comms.root), "goal-checkout")
    denied = core(*command, "add", str(repository.checkout))
    assert denied.returncode != 0
    assert "git remote set-head origin --auto" in denied.stdout + denied.stderr
    repository.git(repository.checkout, "remote", "set-head", "origin", "--auto")
    created = core(*command, "add", str(repository.checkout))
    assert created.returncode == 0, created.stdout + created.stderr
    path = rule.repository_checkout(rule.roots_of(worker), goal.id, repository.github)
    assert repository.git(path, "rev-parse", "HEAD").strip() == repository.git(
        repository.checkout, "rev-parse", "origin/master",
    ).strip()
    assert path in rule.checkout_paths(comms.registry.require(worker.name))
    assert path in rule.checkouts_of(comms.registry.require(worker.name))
    retired = core(*command, "remove", str(repository.checkout))
    assert retired.returncode == 0, retired.stdout + retired.stderr
    assert not path.exists()
    assert path not in rule.checkout_paths(comms.registry.require(worker.name))
