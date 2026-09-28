"""Read-only N/K wake framing from an already committed selected receipt.

The frame tells a one-shot recipient what this wake is for. It grants no
response or file-write authority: the runner separately verifies the sealed
claim, reserves native input, and fences response publication. It deliberately
has no bus/registry/SQLite reads, inbox cursor, or model-launch operation.
"""

from __future__ import annotations

import json
from typing import Literal

from .bus_publication import CommittedInitial, stable_thread_lookup
from .coordination import (
    ResponseObligation,
    WakeClaim,
)
from .coordination_store import IdentityConflict
from .declarations import Thread
from .wake import WakeDecision, derive_exact_reply_target


def render_selected_wake_frame(
    initial: CommittedInitial,
    claim: WakeClaim,
    owner: Thread,
    *,
    phase: Literal["triage", "full"],
    obligation: ResponseObligation | None = None,
) -> str:
    """Render one selected wake; do not manufacture one from message text.

    The caller must first verify the original bus row, sealed SQL receipt and
    live owner. These exact-data checks are defense in depth, not admission.
    A bounded triage has no response obligation until it engages FULL work.
    """
    if (
        phase not in {"triage", "full"}
        or claim.wire_seq != initial.message.seq
        or claim.message_id != initial.message.message_id
        or claim.recipient != owner.name
        or claim.recipient_lookup != stable_thread_lookup(owner.created_at)
    ):
        raise IdentityConflict("wake frame does not match a committed recipient")
    selected = [
        decision
        for recipient, decision in zip(initial.audience.recipients, initial.decisions, strict=True)
        if recipient.recipient_lookup == claim.recipient_lookup
        and recipient.canonical_thread == owner.name
        and isinstance(decision, WakeDecision)
        and decision.recipient == claim.recipient_lookup
        and decision.audience is claim.audience
        and decision.wake_mode is claim.wake_mode
    ]
    if len(selected) != 1:
        raise IdentityConflict("wake frame requires one selected N/K recipient")
    if phase == "triage":
        if not claim.lifecycle.triage_pending or obligation is not None:
            raise IdentityConflict("bounded triage cannot inherit a response obligation")
        expectation = claim.lifecycle.mode.triage_expectation()
        obligation_line = "No response obligation exists until triage engages."
    else:
        target = derive_exact_reply_target(initial.message)
        if (
            not claim.lifecycle.engaged
            or obligation is None
            or not obligation.lifecycle.pending
            or obligation.exact_target != target
            or claim.execution_id != obligation.execution_id
        ):
            raise IdentityConflict("full wake frame requires the current response obligation")
        expectation = claim.lifecycle.mode.full_expectation()
        obligation_line = "you owe a response: " + json.dumps(
            {
                "target": obligation.exact_target,
                "source_seq": claim.wire_seq,
                "execution_id": obligation.execution_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    selected_line = json.dumps(
        {
            "source_seq": claim.wire_seq,
            "claim_id": claim.claim_id,
            "sender": initial.message.sender,
            "target": initial.message.target,
            "audience": selected[0].audience.value,
            "wake_mode": claim.wake_mode.value,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    work_context = json.dumps(
        {
            "name": owner.name,
            "title": owner.title,
            "tags": sorted(owner.tags),
            "original_assignment": owner.task,
            "current_goal": (
                None
                if owner.goal is None
                else {
                    "text": owner.goal.text,
                    "status": owner.goal.state.declared_name,
                    "progress": owner.goal.progress,
                }
            ),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return (
        "── comms: 1 selected ──\n"
        f"selected: {selected_line}\n"
        f"expected: {expectation}\n"
        "── your state ──\n"
        f"{obligation_line}\n"
        f"work_context: {work_context}\n"
        "Judge relevance using your current goal, thread role/title, channel tags and the "
        "new request. The original assignment records how the thread started; an old "
        "bootstrap instruction to wait for a task does not exclude a new relevant request. "
        "A current goal takes precedence over that original assignment. Preserve explicit "
        "goal pauses; answering a coordination question need not resume paused work.\n"
        "This frame is a read-only projection, not file-write permission.\n"
    )
