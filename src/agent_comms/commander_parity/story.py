"""Row 13 runs the shared copied lifecycle, then reads its original Todo facts."""

from dataclasses import replace

from ..commander.order import OrderFile
from ..order_story import FleetStory, OrderStory
from ..todos import SendBackVerdict, StrikeReason, Todo
from ..tool_result import ToolResult
from ..tools import CommsFleetStoryTool, CommsOrderStoryTool, CommsVerdictTool
from .lifecycle import LifecycleCase, LifecycleRun


class StoryCase(LifecycleCase):
    native_owner = CommsOrderStoryTool
    additional_owners = (CommsFleetStoryTool,)
    strike_reason = "The copied worker failed to remove the first unfinished decision."
    back_reason = "The copied worker duplicated the decision instead of repairing its owner."

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        created = run.create()
        verdict = SendBackVerdict(
            created.revision,
            run.commander.incarnation,
            created.held(),
            "story-review",
            StrikeReason(cls.back_reason),
        )
        run.commander.call(CommsVerdictTool(problem=created.id, operation=verdict))
        document = replace(OrderFile.read(run.document.read_text()), problems=(created.id,))
        sent_back = run.commander.comms.goals.todos.get(created.id)
        run.commander.send(
            run.worker,
            run.due,
            document,
            clean=True,
            replaces=sent_back.messages[-1].message_id,
        )
        run.commander.strike(run.worker, created.id, cls.strike_reason)
        saved = run.commander.comms.goals.todos.get(created.id)
        for _ in range(2):
            story = run.commander.call(CommsOrderStoryTool(order_id=saved.order.checked().id))
            assert isinstance(story, OrderStory)
            print(story.line)
        fleet = run.commander.call(CommsFleetStoryTool())
        assert isinstance(fleet, FleetStory)
        print("\n".join(fleet.lines))
        return created, saved, run.commander.comms.goals.todos.get(created.id)

    @classmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str
    ) -> tuple[tuple[str, bool], ...]:
        orders = tuple(result for result in native if isinstance(result, OrderStory))
        fleets = tuple(result for result in native if isinstance(result, FleetStory))
        return (
            ("two actual order reads", len(orders) == 2),
            ("one actual fleet read", len(fleets) == 1),
            ("record unchanged by reads", rows[-2] == rows[-1]),
            ("standalone and implied strike saved", len(rows[-1].strikes) == 2),
            ("send-back saved once", len(rows[-1].verdicts) == 1),
            ("order reads stable", len(orders) == 2 and orders[0] == orders[1]),
            ("strike reason once", bool(orders) and orders[-1].line.count(cls.strike_reason) == 1),
            ("send-back reason once", bool(orders) and orders[-1].line.count(cls.back_reason) == 1),
            ("one fleet line per open order", bool(fleets) and len(fleets[-1].lines) == 1),
            (
                "fleet agrees with order read",
                bool(fleets and orders) and fleets[-1].lines == (orders[-1].line,),
            ),
            ("actual output printed", bool(orders) and orders[-1].line in output),
        )
