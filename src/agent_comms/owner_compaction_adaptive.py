"""ACP owner-turn admission for Pi-native adaptive compaction.

The existing native hard-context backstop remains independent of this trigger.
No input is sent by this module; a committed result resumes via the ordinary
single-send backend path and its strict saved-session reopen.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from .agent_events import AgentEvent
from .backend import PersistentPiSession, _session_revision
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .input_disposition import FutureInputQueue
from .native_pi import NativePiRpcLaunch
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_prepare import NativePreparation
from .owner_compaction_provider import OwnerSummaryOutcome
from .owner_compaction_runtime import (
    SelectedNativeSummary,
    SelectedSummaryDecline,
    compact_owner_once,
)
from .owner_compaction_settings import (
    PiCompactionDecision,
    PiSettingsEvidenceError,
    read_compaction_decision,
)
from .registration import Registration
from .runtime_info import AgentRuntimeInfo
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
    runtime_info: AgentRuntimeInfo | None,
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
    owner, owner_generation = registry.live_owner_with_generation(thread_name)
    selected_native = summary_strategy is None
    if (
        owner.active_turn is None
        or owner.active_turn.id != turn_id
        or owner.session_file is None
        or not owner.model
        or runtime_info is None
        or runtime_info.model != owner.model
        or type(runtime_info.context_size) is not int
        or not 0 < runtime_info.context_size <= 2**53 - 1
        or "/" not in owner.model
    ):
        if selected_native:
            raise PiSettingsEvidenceError("Selected native context must be prepared before input")
        return False
    assert runtime_info is not None
    assert runtime_info.context_size is not None
    context_used = runtime_info.context_used
    if not selected_native and (
        type(context_used) is not int or not 0 <= context_used <= 2**53 - 1
    ):
        return False
    context_window = runtime_info.context_size
    provider, model_id = owner.model.split("/", 1)
    if not provider or not model_id:
        return False
    package = NativePiRpcLaunch.package_for_command(launcher)
    # A selected child owns effective settings including project trust and model
    # overrides. Detached injected strategies still need conservative file proof.
    project_settings = Path(owner.worktree) / ".pi" / "settings.json"
    global_dir = Path(
        os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent"
    ).expanduser()
    if not global_dir.is_absolute():
        raise PiSettingsEvidenceError("Canonical global settings directory required")
    native_config = Path(os.environ.get("AGENT_COMMS_NATIVE_CONFIG_DIR") or global_dir).expanduser()
    if not native_config.is_absolute():
        raise PiSettingsEvidenceError("Canonical native model configuration directory required")
    model_files = tuple(
        dict.fromkeys(
            (
                global_dir / "models.json",
                native_config / "models.json",
                project_settings.with_name("models.json"),
            )
        )
    )
    settings_paths = (
        str(global_dir / "settings.json"),
        str(project_settings),
        *(str(file) for file in model_files),
    )

    def configuration_unbound() -> bool:
        for file in (project_settings, *model_files):
            try:
                file.lstat()
            except FileNotFoundError:
                continue
            return True
        return False

    if selected_native:
        if input_text is None or on_admission is None:
            raise PiSettingsEvidenceError("Selected live Pi summary needs its original-input owner")
        if not persistent.available:
            raise PiSettingsEvidenceError("Selected native session must be prepared before input")
    elif configuration_unbound():
        return False

    async def decision() -> PiCompactionDecision:
        if selected_native:
            return await read_selected_compaction_decision(
                persistent,
                session_file=owner.session_file,
                expected_package=package,
                provider=provider,
                model_id=model_id,
                context_window=context_window,
            )
        if configuration_unbound():
            raise PiSettingsEvidenceError("Adaptive project trust or custom model is not bound")
        return await asyncio.to_thread(
            read_compaction_decision,
            package,
            owner.worktree,
            context_tokens=context_used,
            context_window=context_window,
        )

    settings = await decision()
    if not settings.enabled or not settings.trigger:
        return False
    bridge = await asyncio.to_thread(
        OwnerCompactionCommit, registry.store.path, package, future_queue=future_queue
    )
    if summary_strategy is None:
        assert input_text is not None and on_admission is not None
        revision = _session_revision(owner.session_file)
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
                admission_generation=owner.active_turn.admission_generation,
                correction_witness=f"{owner.active_turn.admission_generation}:{digest.value}",
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
            if result.summary is None:
                if result.decline_reason in {"split_turn", "unsupported"}:
                    return SelectedSummaryDecline(
                        bridge.journal.summaries.get(result.operation_id),
                        identity,
                        result.decline_reason,
                    )
                raise PiSettingsEvidenceError(
                    f"Selected Pi declined summary ({result.decline_reason}); "
                    "original remains unbound"
                )
            return SelectedNativeSummary(
                result.summary.text,
                result.summary.details,
                result.summary.usage,
                bridge.journal.summaries.get(result.operation_id),
                identity,
            )

        summary_strategy = selected_summary

    async def summarize(prepared: NativePreparation) -> OwnerSummaryOutcome:
        # Recheck immediately before paid provider work, then after it. The
        # owner source and ingress remain independently fenced by the bridge.
        current, current_owner_generation = registry.live_owner_with_generation(thread_name)
        if (
            current != owner
            or current_owner_generation != owner_generation
            or await decision() != settings
        ):
            raise RelationViolationError("Adaptive model, owner or settings changed")
        outcome = await summary_strategy(prepared)
        current, current_owner_generation = registry.live_owner_with_generation(thread_name)
        if (
            current != owner
            or current_owner_generation != owner_generation
            or await decision() != settings
        ):
            raise RelationViolationError("Adaptive source changed after summary")
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
