"""Message reads shared by unit observations and teammate answer rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agent_comms.messaging.bus import BusReader
from agent_comms.messaging.messages import Message, MessageKind, NeedsAnswerKind
from agent_comms.planning.orders import Todo
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_identity import ThreadIncarnation

class UnansweredTeammateAsks:
    """Individual requests without an answer to their exact original reference."""

    @classmethod
    def for_debtors(
        cls,
        source: BusReader,
        snapshot: RegistrySnapshot,
        debtors: Sequence[ThreadIncarnation],
        held: Mapping[ThreadIncarnation, Sequence[Todo]],
        before: float,
    ) -> dict[ThreadIncarnation, dict[ThreadIncarnation, tuple[Message, ...]]]:
        """Derive every debtor's unanswered asks from one owner-preserving bus relation."""
        if not held or not debtors:
            return {}
        debtor_by_name = {debtor.name: debtor for debtor in debtors}
        owners_by_name = {owner.name: owner for owner in held}
        selected: dict[str, dict[str, list[Message]]] = {
            debtor.name: {} for debtor in debtors
        }
        aliases = tuple(
            (debtor.name, tuple(sorted(snapshot.names_for(debtor.name))))
            for debtor in debtors
        )
        requesters = tuple(sorted({
            name
            for owner in held
            for name in snapshot.names_for(owner.name)
        }))
        since = min(todo.first_ordered_at for todos in held.values() for todo in todos)
        page_size = 100
        with source.reading() as reader:
            upper = reader.revision() + 1
            while True:
                page, more = reader.unanswered_for_debtors(
                    aliases,
                    requesters,
                    since=since,
                    before=before,
                    upper=upper,
                    limit=page_size,
                    kinds=MessageKind.members_with(NeedsAnswerKind),
                )
                if not page:
                    break
                for debtor, message in page:
                    owner = snapshot.canonical_name(message.sender)
                    if owner in owners_by_name:
                        selected[debtor].setdefault(owner, []).append(message)
                if not more:
                    break
                upper = min(message.seq for _, message in page)
        return {
            debtor_by_name[debtor]: {
                owners_by_name[owner]: tuple(messages)
                for owner, messages in by_owner.items()
            }
            for debtor, by_owner in selected.items()
        }
