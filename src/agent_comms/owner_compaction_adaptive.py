"""ACP owner-turn admission for Pi-native adaptive compaction.

The existing native hard-context backstop remains independent of this trigger.
No input is sent by this module; a committed result resumes via the ordinary
single-send backend path and its strict saved-session reopen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .agent_events import AgentEvent
from .backend import PersistentPiSession
from .input_disposition import FutureInputQueue
from .native_input_owner import RegistryOwner
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_settings import PiSettingsEvidenceError
from .pi_payloads import StateData
from .registration import Registration
from .selected_pi_route import read_selected_compaction_decision
from .selected_source import SelectedAdmissionSource, SessionRevision, SessionRevisionUnavailable
from .selected_summary_admission import SelectedSummaryAdmission
from .thread_identity import TurnId


async def maybe_compact_owner_turn(
    registry: Registration,
    thread_name: str,
    turn_id: str,
    prepared: StateData,
    original_input_keys: tuple[str, ...],
    persistent: PersistentPiSession,
    *,
    input_text: str,
    on_admission: Callable[[SelectedSummaryAdmission], None],
    future_queue: FutureInputQueue | None = None,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
) -> bool:
    """Return False only for a clean trigger skip; errors never dispatch input.

    Called under the ACP session turn lock before any native user start. The
    original batch is durable but provably unbound; the bridge checks every
    original receipt and ingress revision together at native commit.
    """
    snapshot = registry.snapshot()
    try:
        captured = RegistryOwner.capture(snapshot, thread_name, "Selected compaction owner changed")
        turn = captured.require_active_turn()
        if TurnId(turn.id) != TurnId(turn_id):
            raise ValueError("Selected compaction turn changed")
        owner = captured.thread
        session_file = owner.require_saved_session()
        selected = prepared.model.for_compaction(owner.model)
    except ValueError as error:
        raise PiSettingsEvidenceError("Selected native context must be prepared before input") from error
    owner_generation = snapshot.owner_generations[owner.name]
    if not persistent.available:
        raise PiSettingsEvidenceError("Selected native session must be prepared before input")

    package = persistent.custody.idle().child.key[0].package
    settings = await read_selected_compaction_decision(
        persistent, session_file=session_file,
        expected_package=package, selected=selected,
        registry=registry, thread_name=owner.name,
    )
    # Native source budget owns mandatory readiness even when autonomous Pi
    # compaction is disabled. Do not reinterpret its decision in Python.
    if not settings.trigger:
        return False
    bridge = await asyncio.to_thread(
        OwnerCompactionCommit, registry.store.path, package, future_queue=future_queue
    )
    try:
        revision = SessionRevision.observe(session_file).require_available()
    except SessionRevisionUnavailable as error:
        raise PiSettingsEvidenceError("Selected saved source is unavailable") from error
    source = SelectedAdmissionSource.capture(
        owner, TurnId(turn_id), turn.admission_generation, original_input_keys,
        bridge.inputs.read(), input_text, revision,
    )
    result = await bridge.compact_selected(
        owner, owner_generation, persistent, source, selected, settings,
        pending_input_keys=original_input_keys, on_admission=on_admission,
        on_event=on_event,
    )
    return result.adaptive_result()
