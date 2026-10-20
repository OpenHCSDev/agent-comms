import json
import subprocess

from agent_comms.field_codec import FieldCodec
from agent_comms.work_branches import (
    IssueBranchLink,
    PullRequestBranchLink,
    ThreadBranchCheckout,
    UnregisteredBranchCheckout,
    WorkBranches,
)


def _page(field, nodes, after=None):
    return json.dumps(
        {
            "data": {
                "repository": {
                    field: {
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": after is not None, "endCursor": after},
                    }
                }
            }
        }
    )


def test_branches_off_main_carry_only_their_own_pr_issue_and_checkout(comms, tmp_path, monkeypatch):
    comms.registry.declare(
        comms.registry.new_thread(name="unit", tags=frozenset(), worktree=str(tmp_path / "unit"))
    )
    (tmp_path / "unit").mkdir()
    commits = {
        "main": (0, "2026-10-16T10:00:00Z"),
        "fix/a": (3, "2026-10-16T12:00:00Z"),
        "base": (2, "2026-10-16T13:00:00+02:00"),
        "fix/pr": (1, "2026-10-16T09:00:00Z"),
        "fix/issue": (4, "2026-10-16T08:00:00Z"),
        "merged": (0, "2026-10-16T13:00:00Z"),
    }
    pages = {
        "issues": [
            _page(
                "issues",
                [{"number": 7, "linkedBranches": {"nodes": [{"ref": {"name": "fix/issue"}}]}}],
            )
        ],
        "pulls": [
            _page(
                "pullRequests",
                [
                    {"number": 9, "headRefName": "fix/pr", "isCrossRepository": False},
                    {"number": 10, "headRefName": "base", "isCrossRepository": True},
                ],
                after="c1",
            ),
            _page("pullRequests", []),
        ],
    }

    def run(self, *command, input=None):
        if command[:2] == ("git", "ls-remote"):
            return "\n".join(f"{name} refs/heads/{name}" for name in commits)
        if command[:2] == ("git", "cat-file"):
            return "\n".join(f"{name} commit" for name in commits)
        if command[:2] == ("git", "for-each-ref"):
            return "\n".join(f"{name} {date} {ahead} 0" for name, (ahead, date) in commits.items())
        if command[:2] == ("git", "worktree"):
            return (
                f"worktree {tmp_path / 'unit'}\nHEAD 1\nbranch refs/heads/fix/a\n\n"
                f"worktree {tmp_path / 'stray'}\nHEAD 2\nbranch refs/heads/base\n"
            )
        assert command[:3] == ("gh", "api", "graphql")
        return pages["pulls" if "pullRequests(" in command[4] else "issues"].pop(0)

    monkeypatch.setattr(WorkBranches, "repository", property(lambda self: ("Org", "repo")))
    monkeypatch.setattr(WorkBranches, "_run", run)
    branches = WorkBranches(tmp_path).read(comms.registry.snapshot())
    assert [(b.name, b.unmerged_commits, b.links, b.checkouts) for b in branches] == [
        ("fix/a", 3, (), (ThreadBranchCheckout(("unit",), str(tmp_path / "unit")),)),
        ("base", 2, (), (UnregisteredBranchCheckout(str(tmp_path / "stray")),)),
        ("fix/pr", 1, (PullRequestBranchLink(9),), ()),
        ("fix/issue", 4, (IssueBranchLink(7),), ()),
    ]
    assert [FieldCodec.encode(b)["linked"] for b in branches] == [False, False, True, True]
    assert branches[1].committed_at == "2026-10-16T11:00:00Z"


def test_live_remote_heads_fetch_missing_objects_without_using_stale_tracking_refs(
    comms,
    tmp_path,
    monkeypatch,
):
    origin, publisher, reader = (tmp_path / name for name in ("origin.git", "publisher", "reader"))
    for path in (publisher, reader):
        path.mkdir()

    def git(path, *arguments):
        return subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "-c",
                "user.name=probe",
                "-c",
                "user.email=probe@example.invalid",
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    git(publisher, "init", "-q", "-b", "main")
    git(publisher, "remote", "add", "origin", str(origin))
    git(publisher, "commit", "-q", "--allow-empty", "-m", "original main")
    git(publisher, "push", "-q", "origin", "main")
    git(reader, "init", "-q", "-b", "main")
    git(reader, "remote", "add", "origin", str(origin))
    git(reader, "fetch", "-q", "origin", "main")
    git(reader, "checkout", "-q", "-B", "main", "origin/main")
    stale = git(reader, "rev-parse", "origin/main")
    git(publisher, "checkout", "-q", "-b", "fresh")
    git(publisher, "commit", "-q", "--allow-empty", "-m", "fresh branch")
    fresh = git(publisher, "rev-parse", "HEAD")
    git(publisher, "push", "-q", "origin", "fresh")
    git(publisher, "checkout", "-q", "main")
    git(publisher, "commit", "-q", "--allow-empty", "-m", "advanced main")
    main = git(publisher, "rev-parse", "HEAD")
    git(publisher, "push", "-q", "origin", "main")
    run = WorkBranches._run
    fetched = []

    def observe(self, *command, **options):
        if command[:2] == ("git", "fetch"):
            fetched.append(command)
        return run(self, *command, **options)

    # Forge links are independent of the real Git graph under examination.
    monkeypatch.setattr(WorkBranches, "_pages", lambda *args, **kwargs: ())
    monkeypatch.setattr(WorkBranches, "_run", observe)
    branches = WorkBranches(reader).read(comms.registry.snapshot())
    assert [(branch.name, branch.unmerged_commits) for branch in branches] == [("fresh", 1)]
    assert len(fetched) == 1 and set(fetched[0][4:]) == {main, fresh}
    assert git(reader, "rev-parse", "origin/main") == stale
