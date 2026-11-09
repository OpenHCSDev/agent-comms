"""Record, list and complete independent owner requests.

Every request keeps its own id and serving orders. Completing one request never
replaces the teammate's current goal or another request.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Annotated

from agent_comms.messaging.bus_write import HeldBusWrite
from agent_comms.nominal import field_codec
from agent_comms.planning.owner_requests import OwnerRequestId
from agent_comms.team.order import Reasoned, Team, TeamVerb


class RequestVerb(TeamVerb):
    """A verb of `request`."""


class RequestMutation(HeldBusWrite, RequestVerb, declared_name=None):
    """Request mutations use the same bus writer as orders and directives."""

    def apply(self, ctx: Team) -> None:
        self.write_when_open(ctx.comms, partial(super().apply, ctx))


@dataclass(frozen=True, kw_only=True)
class RequestAddVerb(RequestMutation, declared_name="add"):
    help = "record an owner request; prints its id"
    text: Annotated[str, field_codec.Argument(flags=("text",))]

    def run(self, ctx: Team) -> None:
        actor = ctx.comms.registry.require(self.who(ctx)).acting_identity
        request = ctx.comms.goals.todos.add_request(actor, self.text)
        print(request.id.value)


@dataclass(frozen=True, kw_only=True)
class RequestCompleteVerb(Reasoned, RequestMutation, declared_name="complete"):
    help = "close a request; refused while an order serving it is pending"
    id: Annotated[OwnerRequestId, field_codec.Argument(flags=("id",))]

    def run(self, ctx: Team) -> None:
        actor = ctx.comms.registry.require(self.who(ctx)).acting_identity
        ctx.comms.goals.todos.complete_request(actor, self.id, self.why)
        print(f"{self.id.value} done")


class RequestListVerb(RequestVerb, declared_name="list"):
    help = "every open request and its pending orders"

    def run(self, ctx: Team) -> None:
        actor = ctx.comms.registry.require(self.who(ctx)).acting_identity
        store = ctx.comms.goals.todos
        requests = store.open_requests(actor)
        if not requests:
            print("No open request.")
        for request in requests:
            print(f"{request.id.value} {request.text}")
            for todo in store.serving_orders(request.id):
                print(f"  {todo.id} review {todo.review_at.clock()}: {todo.text}")


def main(a: list[str], ctx: Team) -> None:
    RequestVerb.parse(a).apply(ctx)
