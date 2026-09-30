"""ACP owner-turn admission for Pi-native adaptive compaction.

The existing native hard-context backstop remains independent of this trigger.
No input is sent by this module; a committed result resumes via the ordinary
single-send backend path and its strict saved-session reopen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from .agent_events import AgentEvent
from .backend import PersistentPiSession, _session_revision
from .field_codec import FieldCodec
from .input_disposition import FutureInputQueue
from .native_input_owner import RegistryOwner
from .native_pi import NativePiRpcLaunch
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_prepare import NativePreparation
from .owner_compaction_provider import OwnerSummaryOutcome
from .owner_compaction_runtime import compact_owner_once
from .owner_compaction_settings import PiCompactionDecision, PiSettingsEvidenceError
from .pi_payloads import StateData
from .registration import Registration
from .selected_pi_route import read_selected_compaction_decision
from .selected_pi_summary_rpc import SelectedSummarySlot
from .selected_source import SelectedAdmissionSource
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .text_digest import TextDigest
from .thread_identity import TurnId


async def maybe_compact_owner_turn(
    registry: Registration,
    launcher: str,
    thread_name: str,
    turn_id: str,
    prepared: StateData,
    original_input_key: str,
    persistent: PersistentPiSession,
    *,
    summary_strategy: Callable[[NativePreparation], Awaitable[OwnerSummaryOutcome]] | None = None,
    input_text: str | None = None,
    on_admission: Callable[[SelectedSummaryAdmission], None] | None = None,
    future_queue: FutureInputQueue | None = None,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
) -> bool:
    """Return False only for a clean trigger skip; errors never dispatch input.

    Called under the ACP session turn lock before any native user start. The
    original input is durable but provably unbound; the bridge permits only
    that one row and rechecks every ingress revision at native commit.
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
    context_window = selected.context_window
    provider, model_id = selected.provider, selected.model_id
    package = NativePiRpcLaunch.package_for_command(launcher)
    if not persistent.available:
        raise PiSettingsEvidenceError("Selected native session must be prepared before input")
    if summary_strategy is None and (input_text is None or on_admission is None):
        raise PiSettingsEvidenceError("Selected live Pi summary needs its original-input owner")

    # Preserve source-file invalidation across the later native reopen. These
    # paths come from the original prepared child's launch, not display metadata
    # or a detached settings decision. Effective values are still read from Pi.
    environment = persistent.custody.idle().child.key[0].env
    global_dir = Path(environment.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser()
    native_config = Path(environment.get("AGENT_COMMS_NATIVE_CONFIG_DIR") or global_dir).expanduser()
    if not global_dir.is_absolute() or not native_config.is_absolute():
        raise PiSettingsEvidenceError("Prepared native configuration directories must be absolute")
    project_settings = Path(owner.worktree) / ".pi" / "settings.json"
    settings_paths = tuple(dict.fromkeys(map(str, (
        global_dir / "settings.json", project_settings,
        global_dir / "models.json", native_config / "models.json",
        project_settings.with_name("models.json"),
    ))))

    async def decision() -> PiCompactionDecision:
        # Native preparation owns model identity; the same retained child owns
        # effective settings and context use, including injected summary workers.
        return await read_selected_compaction_decision(
            persistent,
            session_file=session_file,
            expected_package=package,
            selected=selected,
        )

    settings = await decision()
    if not settings.enabled or not settings.trigger:
        return False
    bridge = await asyncio.to_thread(
        OwnerCompactionCommit, registry.store.path, package, future_queue=future_queue
    )
    if summary_strategy is None:
        assert input_text is not None and on_admission is not None
        revision = _session_revision(session_file)
        original = bridge.inputs.read().rows.get(original_input_key)
        if revision is None or original is None:
            raise PiSettingsEvidenceError("Selected original input or saved session is unavailable")
        digest = TextDigest.of(input_text)
        identity = SelectedAdmissionIdentity(
            source=SelectedAdmissionSource(
                incarnation=owner.incarnation,
                owner=owner.process_identity,
                turn=TurnId(turn_id),
                ingress_key=original_input_key,
                admission_generation=turn.admission_generation,
                correction_witness=f"{turn.admission_generation}:{digest.value}",
                input_digest=digest,
                original_digest=original.digest,
                reserved_revision=revision,
            ),
            session_revision=revision,
        )

        async def selected_summary(prepared: NativePreparation) -> OwnerSummaryOutcome:
            slot = SelectedSummarySlot(owner.name, prepared.witness.session_id)
            result = await slot.run_selected_summary(
                persistent,
                bridge.journal,
                prepared.witness,
                {
                    "source": FieldCodec.encode(identity.source),
                    "selected": {
                        "provider": provider,
                        "modelId": model_id,
                        "contextWindow": context_window,
                    },
                    "settings": FieldCodec.project(settings, "settings"),
                },
                expected_package=package,
                tokens_before=prepared.tokens_before,
                future_queue=future_queue,
                on_event=on_event,
            )
            return result.adaptive_summary(bridge.journal, identity)

        summary_strategy = selected_summary

    async def summarize(prepared: NativePreparation) -> OwnerSummaryOutcome:
        # Recheck immediately before paid provider work, then after it. The
        # owner source and ingress remain independently fenced by the bridge.
        attestation = owner.compaction_attestation(owner_generation, prepared.witness)
        attestation.require_registry(registry, owner)
        settings.require_current(await decision())
        outcome = await summary_strategy(prepared)
        attestation.require_registry(registry, owner)
        settings.require_current(await decision())
        return outcome

    operation = await compact_owner_once(
        bridge,
        owner,
        owner_generation,
        persistent,
        summarize,
        settings=settings,
        context_window=context_window,
        pending_input_key=original_input_key,
        settings_paths=settings_paths,
        on_admission=on_admission,
        on_event=on_event,
    )
    if operation is None:
        # Pi found no safe cut point. Do not disable the ordinary hard-context
        # backstop or turn this into a request to summarize again.
        return False
    return True
