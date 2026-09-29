# Live turn authority checkpoint, 2026-09-29

## Delivered

- Toad #194 and #196 merged: one Agent-owned managed turn, stable widget source
  binding, stale observed-read retirement, shared native/channel indicator
  construction, and deletion of old writable activity/busy projections.
- Textual #12 merged: computed reads reuse change-only notification. This fixes
  the actual installed startup loop caused by the separator reading busy state.
- Core #401 merged: adaptive compaction completion publishes its committed summary.
- The paired stack pins all three merged versions. Default CLI/ACP/Toad launchers
  now use `/home/ts/.local/share/agent-comms/runtime-turn-authority-20260929`.
- Default `toad-comms` launched through the existing user entrypoint, loaded saved
  agent-comms-ux history and reached Ready after activation.

## Verification

The noneditable installed Toad candidate ran actual UI clicks, ACP, native Pi and
a controlled localhost provider continuously through saved-history startup,
channel-bar opening, unopened-thread and active-participant opening, A/B/A,
preserved rendered bodies, reader position, draft and undo, adaptive fast/reverse/
idle/End scrolling, immediate fork opening before the first native answer, its
first response, and automatic channel notification/reply feedback. The complete
journey passed after migrating both remaining channel indicator callers.

The configured live provider also handled one actual editor-entered prompt on
nra-architecture. Activity and prompt admission followed the active source;
agent-comms-ux remained idle; settlement and A/B/A cleared activity everywhere.
The durable input receipt recorded Started once, and the native assistant saved
the expected reply. Earlier startup attempts had no matching input receipt and
were not replayed. The 31 Textual reactive tests passed, including unchanged reads
from a computed property's own watcher. The configured throbber interval test
passed on 2,000 mounted rows with stable geometry and no idle animation ticks.

The global entrypoint check uses the merged wheel pair. This is not a claim that
the entire performance target or full nominal-refactor goal is complete.

## Remaining owners

- Integration owner: current Codex task. Remaining large-history warm-return and
  tab-speed work continues from the existing preparation/presentation owners;
  measured selection-to-paint checkpoint was 171–220 ms, not below 50 ms.
- Heisenberg: #116 scope/deletion closure, then independent substantial warm-cache/
  tab-speed follow-up. Shared Conversation/CommsChatView integration stays with
  the integration owner until explicit file handoff.
- Existing workers are not interrupted by the launcher switch. Owner/runtime
  lifecycle work remains a separate quiet handoff where needed.

## Resource custody

Owned source lives in persistent worktrees under `/home/ts/wt`. The new immutable
runtime occupies about 59 MiB. Bounded candidate receipts lived under
`/home/ts/.cache/agent-scratch/toad-pr194-turn-20260929`; disposable failed run files
are removed after these concise results are committed. Preserve actual bus state,
native history, saved sessions and uncertain input records. The dirty live source
checkout and its uncommitted stack files were preserved.
