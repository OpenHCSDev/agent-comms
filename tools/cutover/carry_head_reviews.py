"""Carry embedded order-head reviews before the install reconciles TodoStore.

The insert and removal share one transaction, so a completed carry has no old
review left to carry again. Delete this step after the live installation.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal

from pydantic import JsonValue

from agent_comms.field_codec import FieldCodec, GitCommitText
from agent_comms.head_review import PullRequestHead
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.recorded_time import EventTime
from agent_comms.registry_store import RegistryStore
from agent_comms.repository_name import RepositoryName
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import (
    Assignment,
    CheckedProblemOrder,
    HeadReviewRecord,
    ProblemHeadReview,
    ProblemOrder,
    ReviewedHeadVerdict,
    StoredHeadReview,
    Todo,
    TodoStore,
)
from agent_comms.typed_table import Column, TextStorage, TypedRow


@dataclass(frozen=True)
class EmbeddedHeadReview:
    kind: Literal["reviewed_head_verdict"]
    expected_revision: int
    actor: ThreadIncarnation
    previous: Assignment
    at: EventTime
    quote: str
    head: Annotated[str, GitCommitText]

    def record(self, request: PullRequestHead, problem: str) -> HeadReviewRecord:
        return HeadReviewRecord(
            request, self.actor, self.at, ReviewedHeadVerdict(),
            ProblemHeadReview(quote=self.quote, problem=problem, previous=self.previous),
        )


@dataclass(frozen=True)
class EmbeddedReviewSource(TypedRow):
    id: str
    repo: Annotated[RepositoryName, Column(storage=TextStorage)]
    order: ProblemOrder
    prior_orders: tuple[CheckedProblemOrder, ...]
    verdicts: tuple[dict[str, JsonValue], ...]

    def records(self) -> tuple[HeadReviewRecord, ...]:
        links = {link for order in (*self.prior_orders, *self.order.declarations)
                 for link in order.pull_requests}
        if len(links) != 1:
            raise ValueError(f"{self.id}: embedded head review needs one recorded PR, got {links}")
        link, = links
        if link.repo != self.repo:
            raise ValueError(f"{self.id}: embedded head review names another repository")
        reviews = (FieldCodec.decode(EmbeddedHeadReview, wire) for wire in self.verdicts
                   if wire["kind"] == "reviewed_head_verdict")
        return tuple(review.record(PullRequestHead(self.repo, link.number, review.head), self.id)
                     for review in reviews)

    def remaining(self) -> tuple[dict[str, JsonValue], ...]:
        return tuple(wire for wire in self.verdicts if wire["kind"] != "reviewed_head_verdict")


class HeadReviews(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TodoStore,)

    def carry(self, *, apply: bool) -> Carried:
        path = TodoStore.path_in(self.root)
        self.require_confined(self.root, path)
        if not path.exists():
            return Carried(0)
        if apply:
            MaintenanceBarrier(RegistryStore.path_in(self.root)).read().require_held_by(
                MaintenanceHolder.current().nonce, "Carry embedded PR-head reviews")
        with TodoStore.observing(path) as db:
            sources = EmbeddedReviewSource.read(db.execute(
                f'SELECT id,repo,"order",prior_orders,verdicts FROM "{Todo.declared_name}" '
                "WHERE EXISTS (SELECT 1 FROM json_each(verdicts) "
                "WHERE json_extract(value,'$.kind')='reviewed_head_verdict')"))
        prepared = tuple((source, source.records()) for source in sources)
        count = sum(len(records) for _, records in prepared)
        if apply and count:
            with closing(TodoStore.writer(path)) as db, db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                StoredHeadReview.create(db, if_not_exists=True)
                for source, records in prepared:
                    for record in records:
                        StoredHeadReview.insert_values(db, record=record)
                    db.execute(f'UPDATE "{Todo.declared_name}" SET verdicts=? WHERE id=?',
                               (json.dumps(source.remaining()), source.id))
        return Carried(count, detail={"reviews": count, "todos": len(prepared)},
                       rewritten=(path.relative_to(self.root),) if apply and count else ())
