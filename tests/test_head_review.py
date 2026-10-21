"""One verdict family, a real Todo and the external gh executable boundary."""
import json
import os
import time
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from test_holder_standing_rules import held_bus
from source_revision_fixture import SourceRepository
from agent_comms.commander.judge import OkVerb, ShowVerb
from agent_comms.commander.order import Commander, SayVerb
from agent_comms.commander.standing import Unjudged
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.repo import Repo
from agent_comms.todos import (
    AcceptedVerdict, GithubPullRequest, HeadVerdict, OrderVerdict, PullRequestLink,
    RecordPullRequest, StrikeReason, TodoOperation,
)
from agent_comms.change_report import FileLine
from agent_comms.tools import CommsVerdictTool
from agent_comms.turn_digests import OrderEditsRead


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ReviewCase:
    comms: Comms
    ctx: Commander
    binaries: Path
    row: GithubPullRequest
    diff: str

    def write(self, row: GithubPullRequest) -> None:
        self.row = row
        self.binaries.joinpath("pull_requests.json").write_text(json.dumps({
            "repo": self.comms.goals.todos.get("P1").repo.to_wire(),
            "requests": {str(row.number): {"listed": FieldCodec.encode(row), "body": ""}},
            "diff": self.diff,
        }))

    def posts(self) -> list[list[str]]:
        return [call for line in self.binaries.joinpath("calls.jsonl").read_text().splitlines()
                if (call := json.loads(line))[:3] == ["api", "--method", "POST"]]


@pytest.fixture
def review_case(held_bus, tmp_path, monkeypatch):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    binaries.joinpath("gh").symlink_to(Path(__file__).with_name("github_cli_stub.py"))
    monkeypatch.setenv("PATH", os.pathsep.join((str(binaries), os.environ["PATH"])))
    ctx = Commander(commander="lead", root=held_bus.root, state=tmp_path / "state", repos=(),
                    worktrees=tmp_path, scratch=tmp_path, disk=tmp_path)
    todo = held_bus.goals.todos.get("P1")
    held_bus.goals.todos.apply(todo.id, RecordPullRequest(todo.revision, todo.creator_identity,
        PullRequestLink(todo.repo, SourceRepository.number), todo.held().generation))
    checkout = Path(todo.order.checked().check.directory)
    head = Repo.git(checkout, "rev-parse", "feature").strip()
    diff = Repo.git(checkout, "diff", f"main...{head}")
    now = time.time()
    row = GithubPullRequest(number=SourceRepository.number, branch="feature", head=head,
        updated=stamp(now), base="main", created=stamp(now - 3600), head_time=stamp(now - 1200),
        ready_events=(stamp(now - 700),), closed=False, isDraft=False, mergeStateStatus="CLEAN",
        checks="SUCCESS", queued=False, merge=None, merged=None)
    case = ReviewCase(held_bus, ctx, binaries, row, diff)
    case.write(row)
    return case


def test_head_verdict_family_records_status_and_new_push_needs_new_verdict(review_case):
    case = review_case
    violation, = Unjudged.violations(case.comms, "lead")
    assert violation.began() == datetime.strptime(case.row.ready_events[-1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    assert violation.refusal(case.comms, ())
    # The same Todo and immutable head are judged through both declared members.
    for index, verdict_type in enumerate(HeadVerdict.members_with(HeadVerdict), 1):
        todo = case.comms.goals.todos.get("P1")
        parameters = dict(expected_revision=todo.revision, actor=todo.creator_identity,
            previous=todo.held(), quote="Repair the existing owner", head=case.row.head,
            reason=StrikeReason("The required fact is duplicated in this code instead of derived from its declaration"),
            evidence=(FileLine("owner.py", 2),))
        operation = verdict_type(**{field.name: parameters[field.name]
                                   for field in fields(verdict_type) if field.name in parameters})
        result = case.ctx.call(CommsVerdictTool(problem="P1", operation=operation)).todo
        assert result.head_verdict(case.row.head) == operation
        assert Unjudged.violations(case.comms, "lead") == ()
        posts = case.posts()
        assert len(posts) == index
        assert posts[-1][3].endswith("/statuses/" + case.row.head)
        assert "state=" + verdict_type.state in posts[-1]
        assert "context=" + HeadVerdict.context in posts[-1]
        assert result.strike_count == todo.strike_count + len(operation.implied_strikes())
        assert all(read.read() == () for read in OrderEditsRead.for_commander(case.comms, "lead"))
    checkout = Path(result.order.checked().check.directory)
    tree = Repo.git(checkout, "rev-parse", f"{case.row.head}^{{tree}}").strip()
    next_head = Repo.git(checkout, "commit-tree", tree, "-p", case.row.head, "-m", "New review head").strip()
    Repo.git(checkout, "push", "origin", f"{next_head}:refs/pull/{case.row.number}/head")
    case.write(replace(case.row, head=next_head, head_time=stamp(time.time())))
    new, = Unjudged.violations(case.comms, "lead")
    assert new.request.head == next_head
    assert new.refusal(case.comms, ()) == ()
    assert result.head_verdict(operation.head) == operation


def test_overdue_head_refuses_say_but_not_judgment(review_case):
    case = review_case
    with pytest.raises(SystemExit, match=f"PR #{case.row.number}.*head {case.row.head}"):
        SayVerb(units="worker", body_file="unused").apply(case.ctx)
    verb = OkVerb(problem="P1", why="The declared parent now owns the shared behavior",
                  serves="Repair the existing owner", lines=("+class Member(Parent):",))
    verb.apply(case.ctx)
    assert Unjudged.violations(case.comms, "lead") == ()
    assert "state=success" in case.posts()[-1]


def test_show_names_linked_head_and_diff_quotes_cannot_judge_other_code(review_case, capsys):
    case = review_case
    ShowVerb().run(case.ctx)
    assert f"PR #{case.row.number} head {case.row.head}: unreviewed" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="every --line"):
        OkVerb(problem="P1", why="This claim quotes code outside the linked request",
               serves="Repair the existing owner", lines=("not in this diff",)).run(case.ctx)
    assert case.comms.goals.todos.get("P1").verdicts == ()
    assert case.posts() == []
    todo = case.comms.goals.todos.get("P1")
    historical = AcceptedVerdict(todo.revision, todo.creator_identity, todo.held(),
                                "old-edit", quote="Repair the existing owner")
    encoded = FieldCodec.encode(historical)
    assert FieldCodec.decode(OrderVerdict, encoded) == historical
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(TodoOperation, encoded)
