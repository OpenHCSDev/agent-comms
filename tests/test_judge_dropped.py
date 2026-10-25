"""An edit the unit discarded is settled as dropped from git, through the real judge command."""
import json
import time

import pytest

from agent_comms.commander.judge import Edit, main
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.errors import TodoError
from agent_comms.native_tool_call import ToolCallId
from agent_comms.registry_document import RegisteredCommanderProject
from agent_comms.todos import DroppedVerdict, RewrittenVerdict
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.transcript_events import ToolStartTranscript
from agent_comms.turn_item import NativeItemId, NativeTurnId
from source_revision_fixture import SourceRepository

pytest_plugins = ("test_held_problem_orders",)

FOREIGN = "FOREIGN_MARKER_LINE = 'only in the second repository the unit edits'"
LIVE = "LIVE_MARKER_LINE = 'kept on the checkout head'"
DROPPED = "DROPPED_MARKER_LINE = 'only on the discarded formatter branch'"


def committed(repo, name: str, line: str) -> None:
    (repo.checkout / name).write_text(line + "\n")
    SourceRepository.git(repo.checkout, "add", name)
    SourceRepository.git(repo.checkout, "commit", "-q", "-m", f"add {name}")


def recorded_edit(ctx, call_id: ToolCallId, path: str, line: str) -> None:
    formatter_turn_id = NativeTurnId('formatter')
    tool = ToolDeclaration.record(
        "edit", {"path": path, "edits": [{"oldText": "", "newText": line}]}, call_id=call_id)
    worker = ctx.comms.registry.require("worker").incarnation
    ExternalTranscript(ctx.root, worker).finish(
        turn_id=formatter_turn_id,
        item_id=NativeItemId(call_id),
        events=(
            ToolStartTranscript(
                position=0, tool_call_id=call_id, tool_name="edit", timestamp=time.time(), tool=tool
            ),
        ),
    )


def unjudged(ctx) -> dict[str, Edit]:
    return {edit.id: edit for edit in Edit.unjudged(ctx.comms, ctx.commander)}


def judge_dropped(ctx, edit: Edit, line: str) -> None:
    main(["dropped", f"{edit.id}:{edit.code()}", "--why",
          "the unit reset this formatter branch on the corrected order", "--line", line], ctx)


@pytest.fixture
def discarded_and_kept(held_commander):
    ctx = held_commander
    repo = ctx.repos[0]
    rule = ctx.comms.registry.storage_rule
    gone = rule.order_checkout(rule.roots_of(ctx.comms.registry.require("worker")), "P1")
    base = repo.git(repo.checkout, "rev-parse", "--abbrev-ref", "HEAD").strip()
    repo.git(repo.checkout, "checkout", "-q", "-b", "formatter")
    committed(repo, "dropped_module.py", DROPPED)
    repo.git(repo.checkout, "checkout", "-q", base)
    repo.git(repo.checkout, "branch", "-q", "-D", "formatter")
    committed(repo, "kept_module.py", LIVE)
    recorded_edit(ctx, ToolCallId("dropped-call"), str(gone / "dropped_module.py"), DROPPED)
    recorded_edit(ctx, ToolCallId("kept-call"), str(gone / "kept_module.py"), LIVE)
    return ctx


def test_edit_only_on_a_deleted_branch_settles_as_dropped(discarded_and_kept):
    ctx = discarded_and_kept
    edits = unjudged(ctx)
    judge_dropped(ctx, edits["dropped-call"], DROPPED)
    assert "dropped-call" not in unjudged(ctx)
    verdict = ctx.comms.goals.todos.get("P1").verdicts[-1]
    assert isinstance(verdict, DroppedVerdict) and verdict.tool_call_id == "dropped-call"


def test_edit_still_on_the_checkout_head_is_refused(discarded_and_kept):
    ctx = discarded_and_kept
    before = ctx.comms.goals.todos.get("P1")
    with pytest.raises(TodoError, match="kept-call is still on"):
        judge_dropped(ctx, unjudged(ctx)["kept-call"], LIVE)
    assert ctx.comms.goals.todos.get("P1") == before
    assert "kept-call" in unjudged(ctx)


def batch_file(ctx, entries) -> str:
    path = ctx.root / "verdicts.json"
    path.write_text(json.dumps(entries))
    return str(path)


def dropped_entry(edit: Edit, line: str) -> dict:
    return {"kind": "dropped", "ids": [f"{edit.id}:{edit.code()}"], "lines": [line],
            "why": "the unit reset this formatter branch on the corrected order"}


def test_batch_lists_every_refusal_and_writes_nothing(discarded_and_kept):
    ctx = discarded_and_kept
    edits = unjudged(ctx)
    before = ctx.comms.goals.todos.get("P1")
    unread = {"kind": "dropped", "ids": ["dropped-call:0000"], "lines": [DROPPED], "why": "too short"}
    with pytest.raises(SystemExit) as refused:
        main(["batch", batch_file(ctx, [unread, dropped_entry(edits["kept-call"], "no such line")])], ctx)
    assert "entry 1 (dropped)" in str(refused.value.code) and "entry 2 (dropped)" in str(refused.value.code)
    with pytest.raises(TodoError, match="kept-call is still on"):
        main(["batch", batch_file(ctx, [dropped_entry(edits["dropped-call"], DROPPED),
                                        dropped_entry(edits["kept-call"], LIVE)])], ctx)
    assert ctx.comms.goals.todos.get("P1") == before
    assert {"dropped-call", "kept-call"} <= set(unjudged(ctx))


def test_batch_records_its_verdicts_in_one_call(discarded_and_kept, capsys):
    ctx = discarded_and_kept
    edits = unjudged(ctx)
    main(["batch", batch_file(ctx, [dropped_entry(edits["dropped-call"], DROPPED)])], ctx)
    assert "dropped-call" not in unjudged(ctx) and "kept-call" in unjudged(ctx)
    assert "batch: 1 edits; 1 still unjudged" in capsys.readouterr().out


def test_rewritten_settles_only_edits_the_pushed_commit_no_longer_holds(discarded_and_kept, capsys):
    ctx = discarded_and_kept
    repo = ctx.repos[0]
    head = repo.git(repo.checkout, "rev-parse", "HEAD").strip()
    repo.git(repo.checkout, "update-ref", "refs/remotes/origin/unit-work", head)
    main(["rewritten", "worker", "--by", head, "--why",
          "the later pushed commit replaced the formatter module entirely"], ctx)
    left = unjudged(ctx)
    assert "dropped-call" not in left and "kept-call" in left
    verdict = ctx.comms.goals.todos.get("P1").verdicts[-1]
    assert isinstance(verdict, RewrittenVerdict) and verdict.commit == head
    assert "kept-call" in capsys.readouterr().out.splitlines()[-1]


def test_rewritten_refuses_an_unpushed_commit(discarded_and_kept, capsys):
    ctx = discarded_and_kept
    repo = ctx.repos[0]
    head = repo.git(repo.checkout, "rev-parse", "HEAD").strip()
    main(["rewritten", "worker", "--by", head, "--why",
          "the later pushed commit replaced the formatter module entirely"], ctx)
    assert {"dropped-call", "kept-call"} <= set(unjudged(ctx))



def test_rewritten_reads_the_repository_the_edit_was_made_in(discarded_and_kept, proof_build_checkouts):
    """The order checks one repository; the edit was made in the holder's allocation of a second
    one, and a commit pushed there rewrote it."""
    ctx = discarded_and_kept
    other = next(graph.repository for graph in proof_build_checkouts
                 if graph.repository.github != ctx.repos[0].github)
    lead = ctx.comms.registry.require(ctx.commander).incarnation
    ctx.comms.registry.set_commander_project(RegisteredCommanderProject(lead, (*ctx.repos, other)))
    rule = ctx.comms.registry.storage_rule
    allocated = rule.repository_checkout(rule.roots_of(ctx.comms.registry.require("worker")),
                                         "P1", other.github)
    recorded_edit(ctx, "other-call", str(allocated / "other_module.py"), FOREIGN)
    committed(other, "other_module.py", "REPLACED_MARKER_LINE = 'what the later commit wrote'")
    other.git(other.checkout, "push", "-q", "origin", "HEAD:refs/heads/unit-rewrite")
    head = other.git(other.checkout, "rev-parse", "HEAD").strip()
    why = "the later pushed commit replaced the marker module entirely"
    main(["rewritten", "worker", "--by", head, "--why", why], ctx)
    assert "other-call" not in unjudged(ctx)
    verdict = ctx.comms.goals.todos.get("P1").verdicts[-1]
    assert isinstance(verdict, RewrittenVerdict) and verdict.reason == why
