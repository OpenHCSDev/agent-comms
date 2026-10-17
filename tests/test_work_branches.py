from agent_comms.storage_rules import StorageRule
import json

from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from agent_comms.work_branches import (
    IssueBranchLink, PullRequestBranchLink, ThreadBranchCheckout, UnregisteredBranchCheckout,
    WorkBranches,
)


def _ref(name, unmerged, date, *pulls):
    return {"name": name, "target": {"committedDate": date}, "compare": {"behindBy": unmerged},
            "associatedPullRequests": {"nodes": [{"number": n, "headRefName": head}
                                                 for n, head in pulls]}}


def _page(field, nodes, after=None):
    return json.dumps({"data": {"repository": {field: {
        "nodes": nodes, "pageInfo": {"hasNextPage": after is not None, "endCursor": after}}}}})


def test_branches_off_main_carry_only_their_own_pr_issue_and_checkout(comms, tmp_path, monkeypatch):
    comms.registry.declare(Thread.create(name="unit", tags=frozenset(), worktree=str(tmp_path / "unit"), storage_rule=StorageRule()))
    (tmp_path / "unit").mkdir()
    answers = {
        "repo": "Org/repo\n",
        "worktree": (f"worktree {tmp_path / 'unit'}\nHEAD 1\nbranch refs/heads/fix/a\n\n"
                     f"worktree {tmp_path / 'stray'}\nHEAD 2\nbranch refs/heads/base\n"),
        "issues": [_page("issues", [{"number": 7, "linkedBranches": {
            "nodes": [{"ref": {"name": "fix/issue"}}]}}])],
        "refs": [
            _page("refs", [_ref("main", 0, "2026-10-16T10:00:00Z"),
                           _ref("fix/a", 3, "2026-10-16T12:00:00Z"),
                           # PR 9 only uses base as its base branch: base is unlinked.
                           _ref("base", 2, "2026-10-16T11:00:00Z", (9, "fix/pr"))], after="c1"),
            _page("refs", [_ref("fix/pr", 1, "2026-10-16T09:00:00Z", (9, "fix/pr")),
                           _ref("fix/issue", 4, "2026-10-16T08:00:00Z"),
                           _ref("merged", 0, "2026-10-16T13:00:00Z")]),
        ],
    }

    def run(self, *command):
        if command[:3] == ("gh", "repo", "view"):
            return answers["repo"]
        if command[:2] == ("git", "worktree"):
            return answers["worktree"]
        query = command[4]
        return answers["refs" if "refs(" in query else "issues"].pop(0)

    monkeypatch.setattr(WorkBranches, "_run", run)
    branches = WorkBranches(tmp_path).read(comms.registry.snapshot())

    assert [(b.name, b.unmerged_commits, b.links, b.checkouts) for b in branches] == [
        ("fix/a", 3, (), (ThreadBranchCheckout(("unit",), str(tmp_path / "unit")),)),
        ("base", 2, (), (UnregisteredBranchCheckout(str(tmp_path / "stray")),)),
        ("fix/pr", 1, (PullRequestBranchLink(9),), ()),
        ("fix/issue", 4, (IssueBranchLink(7),), ()),
    ]
    assert [FieldCodec.encode(b)["linked"] for b in branches] == [False, False, True, True]
