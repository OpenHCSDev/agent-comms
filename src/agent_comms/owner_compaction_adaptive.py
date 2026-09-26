"""Opt-in ACP owner-turn admission for Pi-native adaptive compaction.

The existing native hard-context backstop remains independent of this trigger.
No input is sent by this module; a committed result resumes via the ordinary
single-send backend path and its strict saved-session reopen.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from .backend import PersistentPiSession
from .compaction_journal import CompactionJournalError
from .declarations import AgentRuntimeInfo, RelationViolationError, ThreadRegistry
from .native_session_reopen import package_for_launcher
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_provider import summarize_native
from .owner_compaction_runtime import PreparedOwnerSummary, compact_owner_once
from .owner_compaction_settings import (
    PiCompactionDecision,
    PiSettingsEvidenceError,
    read_compaction_decision,
)


async def maybe_compact_owner_turn(
    registry: ThreadRegistry,
    launcher: str,
    thread_name: str,
    turn_id: str,
    runtime_info: AgentRuntimeInfo | None,
    original_input_key: str,
    persistent: PersistentPiSession,
    *,
    summary_strategy: Callable[[PreparedOwnerSummary], Awaitable[str]] | None = None,
) -> bool:
    """Return False only for a clean trigger skip; errors never dispatch input.

    Called under the ACP session turn lock before any native user start. The
    original input is durable but provably unbound; the bridge permits only
    that one row and rechecks every ingress revision at native commit.
    """
    owner, epoch = registry.live_owner_with_epoch(thread_name)
    goal = owner.goal
    if (
        goal is None
        or not goal.active
        or owner.active_turn is None
        or owner.active_turn.id != turn_id
        or owner.session_file is None
        or not owner.model
        or runtime_info is None
        or runtime_info.model != owner.model
        or type(runtime_info.context_used) is not int
        or type(runtime_info.context_size) is not int
        or not 0 <= runtime_info.context_used <= 2**53 - 1
        or not 0 < runtime_info.context_size <= 2**53 - 1
        or "/" not in owner.model
    ):
        return False
    assert runtime_info is not None
    assert runtime_info.context_used is not None
    assert runtime_info.context_size is not None
    context_used = runtime_info.context_used
    context_window = runtime_info.context_size
    provider, model_id = owner.model.split("/", 1)
    if not provider or not model_id:
        return False
    package = package_for_launcher(launcher)
    # The reader can merge project settings with projectTrusted:true, but the
    # ACP owner has no durable proof of Pi's live project trust decision.
    # Until that decision is bound, NEVER pay when a project settings file
    # exists. Absence itself is captured and rechecked at native commit.
    project_settings = Path(owner.worktree) / ".pi" / "settings.json"
    global_dir = Path(
        os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent"
    ).expanduser()
    if not global_dir.is_absolute():
        raise PiSettingsEvidenceError("Canonical global settings directory required")
    model_files = (global_dir / "models.json", project_settings.with_name("models.json"))
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

    if configuration_unbound():
        return False

    async def decision() -> PiCompactionDecision:
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
    bridge = await asyncio.to_thread(OwnerCompactionCommit, registry._path, package)

    async def summarize(prepared: PreparedOwnerSummary) -> str:
        # Recheck immediately before paid provider work, then after it. The
        # owner source and ingress remain independently fenced by the bridge.
        current, current_epoch = registry.live_owner_with_epoch(thread_name)
        if current != owner or current_epoch != epoch or await decision() != settings:
            raise RelationViolationError("Adaptive model, owner or settings changed")
        if summary_strategy is None:
            text = await summarize_native(
                package,
                prepared.preparation,
                provider=provider,
                model_id=model_id,
                context_window=context_window,
                reserve_tokens=settings.reserve_tokens,
                keep_recent_tokens=settings.keep_recent_tokens,
            )
        else:
            text = await summary_strategy(prepared)
        current, current_epoch = registry.live_owner_with_epoch(thread_name)
        if current != owner or current_epoch != epoch or await decision() != settings:
            raise RelationViolationError("Adaptive source changed after summary")
        return text

    operation = await compact_owner_once(
        bridge,
        owner,
        epoch,
        persistent,
        summarize,
        keep_recent_tokens=settings.keep_recent_tokens,
        pending_input_key=original_input_key,
        settings_paths=settings_paths,
        allow_split_turn=False,
    )
    if operation is None:
        # Pi found no safe cut point. Do not disable the ordinary hard-context
        # backstop or turn this into a request to summarize again.
        return False
    if operation.status != "committed":
        raise CompactionJournalError(
            f"Adaptive native operation {operation.commit_id} is {operation.status}; "
            "reconcile exact ID before any new input"
        )
    return True
