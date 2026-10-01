"""Read-only N/K wake framing from an already committed selected receipt.

The frame tells a one-shot recipient what this wake is for. It grants no
response or file-write authority: the runner separately verifies the sealed
claim, reserves native input, and fences response publication. It deliberately
has no bus/registry/SQLite reads, inbox cursor, or model-launch operation.
"""

from __future__ import annotations

import json
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.responses import ResponseObligation

from .bus_publication import CommittedDelivery
from .threads import Thread


def render_selected_wake_frame(
    initial: CommittedDelivery,
    assignment: WakeAssignment,
    owner: Thread,
    *,
    obligation: ResponseObligation | None = None,
) -> str:
    """Render one selected wake; do not manufacture one from message text.

    The caller must first verify the original bus row, sealed SQL receipt and
    live owner. These exact-data checks are defense in depth, not admission.
    A bounded triage has no response obligation until it engages FULL work.
    """
    return render_selected_batch_frame(((initial, assignment),), owner, obligation=obligation)


def render_selected_batch_frame(sources, owner: Thread, *, obligation=None) -> str:
    """Validate every original and render common owner context exactly once."""
    selected = []
    for initial, assignment in sources:
        assignment.require_selected_source(initial, owner)
        expectation, obligation_line = assignment.lifecycle.wake_frame(initial.message, obligation)
        selected.append({
            "source_seq": assignment.wire_seq,
            "claim_id": assignment.assignment_id,
            "sender": initial.message.sender,
            "target": initial.message.target,
            "audience": assignment.audience.value,
            "wake_mode": assignment.lifecycle.mode.declared_name,
            "expectation": expectation,
            "response_obligation": obligation_line,
            "body": initial.message.body,
        })
    selected_line = json.dumps(
        selected,
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
        f"── comms: {len(selected)} selected ──\n"
        f"selected: {selected_line}\n"
        "── your state ──\n"
        f"work_context: {work_context}\n"
        "Judge relevance using your current goal, thread role/title, channel tags and the "
        "new request. The original assignment records how the thread started; an old "
        "bootstrap instruction to wait for a task does not exclude a new relevant request. "
        "A current goal takes precedence over that original assignment. Preserve explicit "
        "goal pauses; answering a coordination question need not resume paused work.\n"
        "This frame is a read-only projection, not file-write permission.\n"
    )
