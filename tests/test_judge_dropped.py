"""An edit the unit discarded is settled as dropped from git, through the real judge command."""
import time

import pytest

from agent_comms.commander.judge import Edit, main
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.todos import DroppedVerdict, TodoError
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.transcript_events import ToolStartTranscript
from agent_comms.turn_item import NativeItemId, NativeTurnId
pytest_plugins = ("test_held_problem_orders",)

LIVE = "LIVE_MARKER_LINE = 'kept on the checkout head'"
DROPPED = "DROPPED_MARKER_LINE = 'only on the discarded formatter branch'"


def committed(repo, name: str, line: str) -> None:
    (repo.checkout / name).write_text(line + "\n")
    repo.git(repo.checkout, "add", name)
    repo.git(repo.checkout, "commit", "-q", "-m", f"add {name}")


def recorded_edit(ctx, call_id: str, path: str, line: str) -> None:
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
    gone = ctx.root / "removed-unit-worktree"
    base = repo.git(repo.checkout, "rev-parse", "--abbrev-ref", "HEAD").strip()
    repo.git(repo.checkout, "checkout", "-q", "-b", "formatter")
    committed(repo, "dropped_module.py", DROPPED)
    repo.git(repo.checkout, "checkout", "-q", base)
    repo.git(repo.checkout, "branch", "-q", "-D", "formatter")
    committed(repo, "kept_module.py", LIVE)
    recorded_edit(ctx, "dropped-call", str(gone / "dropped_module.py"), DROPPED)
    recorded_edit(ctx, "kept-call", str(gone / "kept_module.py"), LIVE)
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
