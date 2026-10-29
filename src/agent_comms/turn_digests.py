"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from pydantic import JsonValue

from .bus import Bus
from .context_store import ContextStore
from .nominal.declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .external_transcript import ExternalTranscript, StoreRead
from .nominal.field_codec import FieldCodec
from .held_orders import HeldOrders, LiveHold
from .order_work import OrderWork
from .repo import Checkout, Repo
from .storage_rules import CheckoutAllocation, StorageRule
from .thread_execution import ConversationPreparation
from .thread_identity import ThreadIncarnation
from .threads import Thread
from .todos import ProgressRead, PullRequestLink, Todo, TodoStore
from .tool_declaration import EditCall
from .tool_result import ToolResult
from .transcript_events import IncomingTranscript, TurnDigest
from .turn_item import NativeTurnId

if TYPE_CHECKING:
    from .comms import Comms
    from .registry_document import RegistrySnapshot
    from .repository_name import RepositoryName
    from .todos import OrderProgress
    from .transcript_sources import TranscriptSource


class TurnView(DeclaredFamily, affix="TurnView"):
    """The original facts or a bounded activity projection of the same read."""

    @classmethod
    @abstractmethod
    def project(cls, answer: ThreadTurns) -> JsonValue: ...


class ActivityTurnView(TurnView):
    turn_limit: ClassVar[int] = 5

    @classmethod
    def project(cls, answer: ThreadTurns) -> JsonValue:
        selected = answer.turns[:cls.turn_limit]
        page = replace(answer, turns=selected, older=answer.older.older(selected))
        return FieldCodec.project(page, "activity")


class OriginalTurnView(TurnView):
    @classmethod
    def project(cls, answer: ThreadTurns) -> JsonValue:
        return FieldCodec.encode(answer)


@dataclass(frozen=True)
class ThreadTurns(ToolResult):
    thread: ThreadIncarnation
    turns: tuple[TurnDigest, ...]
    older: TurnBound
    detail: type[TurnView] = ActivityTurnView

    def to_wire(self) -> JsonValue:
        return self.detail.project(self)


@dataclass(frozen=True)
class UnjudgedEdits(ToolResult):
    edits: tuple[EditCall, ...]


class TurnBound(DeclaredFamily):
    """Which of a unit's turns, read newest first, a reader returns."""

    def skips(self, turn: TurnDigest) -> bool:
        return False

    def page_count(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(turns)

    def older(self, turns: tuple[TurnDigest, ...]) -> TurnBound:
        """A recorded identity continues even across undated or equal-time turns."""
        if not turns:
            return self
        return TurnsOlderThan(turn_id=turns[-1].turn_id, count=self.page_count(turns))

    @abstractmethod
    def includes(self, turn: TurnDigest) -> bool:
        """Whether this turn belongs; the first turn refused ends the read."""

    @abstractmethod
    def full(self, taken: int) -> bool:
        """Whether ``taken`` turns end the read."""

    def take(self, turns: Iterable[TurnDigest]) -> tuple[TurnDigest, ...]:
        """The bounded turns of a newest-first stream, reading no further than the bound needs."""
        taken: list[TurnDigest] = []
        for turn in turns:
            if self.skips(turn):
                continue
            if not self.includes(turn):
                break
            taken.append(turn)
            if self.full(len(taken)):
                break
        return tuple(taken)


@dataclass(frozen=True)
class LastTurns(TurnBound):
    count: int = 5

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("last must be positive")

    def includes(self, turn: TurnDigest) -> bool:
        return True

    def full(self, taken: int) -> bool:
        return taken >= self.count

    def page_count(self, turns: tuple[TurnDigest, ...]) -> int:
        return self.count


@dataclass(frozen=True)
class TurnsBefore(LastTurns):
    at: float = field(kw_only=True)

    def skips(self, turn: TurnDigest) -> bool:
        return not turn.starts_before(self.at)


@dataclass(frozen=True)
class TurnsOlderThan(LastTurns):
    """Read older than an already returned original turn, not an offset from the newest."""

    turn_id: NativeTurnId = field(kw_only=True)

    def take(self, turns: Iterable[TurnDigest]) -> tuple[TurnDigest, ...]:
        remaining = iter(turns)
        for turn in remaining:
            if turn.turn_id == self.turn_id:
                return super().take(remaining)
        raise ToolRefusedError(f"Continuation turn {self.turn_id!r} is not in the recorded transcript.")


@dataclass(frozen=True)
class TurnsSince(TurnBound):
    at: float

    def includes(self, turn: TurnDigest) -> bool:
        return not turn.before(self.at)

    def full(self, taken: int) -> bool:
        return False


@dataclass(frozen=True)
class TranscriptRead[Result](ConversationPreparation):
    """The original native/external source selection, shared by typed readers."""

    root: Path
    thread: Thread
    bound: TurnBound

    def read(self) -> Result:
        return self.thread.execution.prepare_conversation(self)

    def native(self) -> Result:
        source = self.thread.runtime.history_source(self.root, self.thread.incarnation)
        return self.from_native(source, source.turns(self.bound))

    def from_native(self, source: TranscriptSource, turns: tuple[TurnDigest, ...]) -> Result:
        return self.from_turns(turns)

    def external(self) -> Result:
        with ExternalTranscript(self.root, self.thread.incarnation).reading() as source:
            return self.from_external(source)

    @abstractmethod
    def from_turns(self, turns: tuple[TurnDigest, ...]) -> Result: ...

    def from_external(self, source: StoreRead) -> Result:
        return self.from_turns(source.turns(self.bound))


class TurnDigestRead(TranscriptRead[ThreadTurns]):
    """Read this unit's own records, never its inherited conversation display."""

    def from_native(self, source: TranscriptSource, turns: tuple[TurnDigest, ...]) -> ThreadTurns:
        """A woken input reads as the messages its wake selected, from their bus records."""
        recorded = source.recorded_contexts(
            ContextStore(self.root), tuple(turn.turn_id for turn in turns))
        selected = {turn_id: manifest.wire_sources() for turn_id, manifest in recorded.items()}
        references = tuple(dict.fromkeys(ref for refs in selected.values() for ref in refs))
        deliveries = (Bus(Bus.path_in(self.root)).deliveries_for_references(references)
                      if references else ())
        # The owner's own sent outcomes share the wake's provenance; their sender tells them apart.
        incoming = {delivery.reference: IncomingTranscript.of(delivery, reader=self.thread.name)
                    for delivery in deliveries
                    if delivery.sender_lookup != self.thread.incarnation.lookup}
        return self.from_turns(tuple(
            turn.with_selected(chosen) if (chosen := tuple(
                incoming[ref] for ref in selected.get(turn.turn_id, ()) if ref in incoming))
            else turn
            for turn in turns))

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> ThreadTurns:
        return ThreadTurns(self.thread.incarnation, turns, self.bound.older(turns))


@dataclass(frozen=True)
class OrderRead[Result](TranscriptRead[Result]):
    bound: TurnsSince
    todo: Todo


@dataclass(frozen=True)
class OrderEditRead[Result](OrderRead[Result]):
    """A held order's edits: the ones its workplaces own that no verdict names."""
    workplaces: EditWorkplaces

    @classmethod
    def for_commander(cls, comms: Comms, commander: str) -> Iterator[Self]:
        snapshot = comms.registry.snapshot()
        held = HeldOrders.of(comms.goals.todos, snapshot, commander)
        history = comms.goals.todos.history_for_commander(snapshot.require(commander).incarnation)
        pulls = ProgressRead(history)
        workplaces = EditWorkplaces.capture(comms, snapshot, held.live, history, pulls)
        yield from cls.judgeable(
            comms.root, held.live, pulls.progress(held.todos, comms.registry), workplaces)

    @classmethod
    def judgeable(cls, root: Path, holds: Iterable[LiveHold],
                  progress: Mapping[str, OrderProgress],
                  workplaces: Mapping[ThreadIncarnation, EditWorkplaces]) -> Iterator[Self]:
        """The reads a verdict can still clear: an order whose PR merged has no head left to
        judge, so its edits are nobody's review and its transcript is never read."""
        return (read for read in cls.for_orders(root, holds, workplaces)
                if progress[read.todo.id].judgeable)

    @classmethod
    def for_orders(cls, root: Path, holds: Iterable[LiveHold],
                   workplaces: Mapping[ThreadIncarnation, EditWorkplaces]) -> Iterator[Self]:
        """Read each live hold's transcript from its canonical review cut; each edit is
        read under the one order its workplace names."""
        store = TodoStore(root)
        for hold in holds:
            yield cls(
                root, hold.holder, TurnsSince(store.unjudged_since(hold.todo)), hold.todo,
                workplaces[hold.holder.incarnation],
            )

    def owned(self, edits: Iterable[EditCall]) -> tuple[EditCall, ...]:
        judged = frozenset(self.todo.judged_calls)
        return tuple(replace(edit, problem=self.todo.id) for edit in edits
                     if edit.id not in judged
                     and self.workplaces.problems_for(edit.path) == frozenset((self.todo.id,)))

    def edits_from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        since = self.bound.at
        edits = {edit.id: edit for turn in reversed(turns)
                 for event in turn.events
                 for edit in event.edit_calls(self.thread.name, self.todo.id, since)}
        return self.owned(reversed(edits.values()))


@dataclass(frozen=True)
class EditWorkplaces:
    """One read's derived boundaries. Allocations take precedence over generic source roots;
    zero or multiple candidates never select a held order. Original edit records stay put."""

    rule: StorageRule
    owners: Mapping[Path, Thread]
    allocated: Mapping[Path, frozenset[str]]
    sources: Mapping[Path, frozenset[str]]

    @classmethod
    def capture(cls, comms: Comms, snapshot: RegistrySnapshot, holds: Iterable[LiveHold],
                history: tuple[Todo, ...], pulls: ProgressRead
                ) -> dict[ThreadIncarnation, EditWorkplaces]:
        """Acquire source/forge facts once, then match edits without Git or network calls."""
        holds = tuple(holds)
        if not holds:
            return {}
        repositories = snapshot.commander_project.require().repos
        registered = frozenset(repo.github for repo in repositories)
        rule = StorageRule.load(comms.root)
        todos = {todo.id: todo for todo in history}
        declared = {
            todo.id: tuple(dict.fromkeys(repository for order in todo.order.declarations
                                        for repository in order.source_repositories()))
            for todo in history
        }
        trees: dict[RepositoryName, dict[str, tuple[str, ...]]] = {}
        for repo in repositories:
            branches = trees.setdefault(repo.github, {})
            for branch, worktrees in pulls.worktrees(repo).items():
                branches[branch] = (*branches.get(branch, ()), *worktrees)
        listed = {Path(path) for branches in trees.values()
                  for worktrees in branches.values() for path in worktrees}
        explicit = {order.check.directory for hold in holds
                    for order in hold.todo.order.declarations}
        paths = listed | explicit
        for hold in holds:
            roots = rule.roots_of(hold.holder)
            paths.add(rule.order_checkout(roots, hold.todo.id))
            paths.update(rule.repository_checkout(roots, hold.todo.id, repo)
                         for repo in declared[hold.todo.id])
        # Qualify only captured Git roots, never nearest parents of individual events.
        # An alias cannot manufacture another problem allocation around the same source.
        present = Checkout.git_roots(*paths)
        actual: dict[Path, Repo] = {}
        for path in present:
            if path.is_absolute() and Checkout(path).resolved().path == path:
                repository = Repo.of(path)
                if repository.github in registered:
                    actual[path] = repository
        # An explicit retained source may be a separate clone, not a worktree of the
        # registered checkout. Read its existing worktree owner once, never per edit;
        # only that already-declared root joins the branch facts, not arbitrary siblings.
        for path in explicit - listed:
            if path in actual:
                branches = trees[actual[path].github]
                for branch, worktrees in pulls.worktrees(actual[path]).items():
                    if str(path) in worktrees:
                        branches[branch] = (*branches.get(branch, ()), str(path))
        links = tuple(dict.fromkeys(link for hold in holds
                                    for order in hold.todo.order.declarations
                                    for link in order.pull_requests))
        requests = {PullRequestLink(repo.github, row.number): row
                    for repo in repositories
                    for row in pulls.rows(repo.github, tuple(
                        link.number for link in links if link.repo == repo.github))}
        owners = {rule.order_checkout(rule.roots_of(thread), ''): thread
                  for thread in snapshot.threads.values()}
        allocations: dict[Path, tuple[tuple[Thread, CheckoutAllocation], ...]] = {}
        for path in present:
            candidates = tuple((thread, allocation)
                               for prefix, thread in owners.items()
                               if path.parent == prefix.parent and path.name.startswith(prefix.name)
                               for allocation in CheckoutAllocation.for_path(rule, thread, path)
                               if allocation.problems())
            if candidates:
                allocations[path] = candidates
        # Every recognized allocation reserves its boundary, even without registered Git proof.
        allocated: dict[Path, frozenset[str]] = {path: frozenset() for path in allocations}
        for path, repository in actual.items():
            if path in allocations:
                allocated[path] = frozenset(
                    problem for thread, allocation in allocations[path]
                    for problem in allocation.problems()
                    if problem in todos
                    and repository.github in allocation.source_repositories(
                        todos[problem].repo, declared[problem])
                    and allocation.owns_declared(rule, thread, path, repository.github))
        workplaces: dict[ThreadIncarnation, EditWorkplaces] = {}
        for holder in dict.fromkeys(hold.holder.incarnation for hold in holds):
            sources: dict[Path, set[str]] = {}
            for hold in holds:
                if hold.holder.incarnation != holder:
                    continue
                todo = hold.todo
                linked = tuple(link for order in todo.order.declarations
                               for link in order.pull_requests)
                for order in todo.order.declarations:
                    path = order.check.directory
                    # Explicit pre-PR source is valid only for its primary actual repository,
                    # and ceases being a generic workplace once that source has a PR link.
                    if (path in actual and actual[path].github == todo.repo
                            and not any(link.repo == todo.repo for link in linked)):
                        sources.setdefault(path, set()).add(todo.id)
                for link in linked:
                    if link not in requests:
                        continue  # Unregistered repositories cannot qualify a source boundary.
                    for worktree_path in trees[link.repo].get(requests[link].branch, ()):
                        root = Path(worktree_path)
                        if root in actual and actual[root].github == link.repo:
                            sources.setdefault(root, set()).add(todo.id)
            workplaces[holder] = cls(rule, owners, allocated, {
                path: frozenset(problems) for path, problems in sources.items()})
        return workplaces

    def problems_for(self, path: str) -> frozenset[str]:
        original = Path(path)
        if not original.is_absolute():
            return frozenset()
        # Resolve the edited path once, not its missing leaf with Git. Lexical membership
        # rejects aliases into a checkout; resolved membership rejects symlink escapes.
        resolved = original.resolve()
        parts = (original.relative_to(self.rule.checkout_base).parts
                 if original.is_relative_to(self.rule.checkout_base) else ())
        boundary = self.rule.checkout_base.joinpath(*parts[:1])
        reserved = any(allocation.problems()
                       for prefix, thread in self.owners.items()
                       if boundary.parent == prefix.parent and boundary.name.startswith(prefix.name)
                       for allocation in CheckoutAllocation.for_path(self.rule, thread, boundary))
        # Even a missing/released/unknown allocation reserves its lexical boundary. Only
        # captured actual Git identity may supply an owner; absence never enables spill.
        candidates = ({boundary: self.allocated.get(boundary, frozenset())}
                      if reserved else self.sources)
        return frozenset(problem for root, problems in candidates.items()
                         if original.is_relative_to(root) and resolved.is_relative_to(root)
                         for problem in problems)


class OrderEditsRead(OrderEditRead[tuple[EditCall, ...]]):
    """Original unjudged calls; external events are selected before decoding."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        return self.edits_from_turns(turns)

    def from_external(self, source: StoreRead) -> tuple[EditCall, ...]:
        return self.owned(source.unjudged(self.todo, self.bound.at))


class OrderEditCountRead(OrderEditRead[int]):
    """Load counts the edits each order owns; ownership reads each edit's path."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(self.edits_from_turns(turns))

    def from_external(self, source: StoreRead) -> int:
        return len(self.owned(source.unjudged(self.todo, self.bound.at)))


@dataclass(frozen=True)
class OrderToolCallCountRead(OrderRead[int]):
    """Original tool starts since a captured head/order cut, without loading external bodies."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(OrderWork.read(
            self.todo.id, self.thread.incarnation, self.bound.at, turns,
        ).calls)

    def from_external(self, source: StoreRead) -> int:
        return source.tool_call_count(self.bound.at)
