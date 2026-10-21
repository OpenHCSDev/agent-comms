"""Record, list and complete independent owner requests.

Every request keeps its own id and serving orders. Completing one request never
replaces the commander's current goal or another request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms import field_codec
from agent_comms.commander.order import Commander, CommanderVerb
from agent_comms.owner_requests import OwnerRequestId


class RequestVerb(CommanderVerb):
    """A verb of `request`."""


@dataclass(frozen=True, kw_only=True)
class RequestAddVerb(RequestVerb, declared_name="add"):
    help = "record an owner request; prints its id"
    text: Annotated[str, field_codec.Argument(flags=("text",))]

    def run(self, ctx: Commander) -> None:
        commander = ctx.comms.registry.require(self.who(ctx)).incarnation
        request = ctx.comms.goals.todos.add_request(commander, self.text)
        print(request.id.value)


@dataclass(frozen=True, kw_only=True)
class RequestCompleteVerb(RequestVerb, declared_name="complete"):
    help = "close a request; refused while an order serving it is pending"
    id: Annotated[OwnerRequestId, field_codec.Argument(flags=("id",))]
    why: Annotated[str, field_codec.Argument(flags=("--why",))]

    def run(self, ctx: Commander) -> None:
        commander = ctx.comms.registry.require(self.who(ctx)).incarnation
        ctx.comms.goals.todos.complete_request(commander, self.id, self.why)
        print(f"{self.id.value} done")


class RequestListVerb(RequestVerb, declared_name="list"):
    help = "every open request and its pending orders"

    def run(self, ctx: Commander) -> None:
        commander = ctx.comms.registry.require(self.who(ctx)).incarnation
        store = ctx.comms.goals.todos
        requests = store.open_requests(commander)
        if not requests:
            print("No open request.")
        for request in requests:
            print(f"{request.id.value} {request.text}")
            for todo in (todo for todo in store.for_request(commander, request.id.value)
                         if todo.pending):
                print(f"  {todo.id} review {todo.review_at.clock()}: {todo.text}")


def main(a: list[str], ctx: Commander) -> None:
    RequestVerb.parse(a, __doc__).apply(ctx)
