# Core observation preparation

Owner: core-retry. Worktree: `/home/ts/wt/core-retry-observation`.

The existing `ObservationService` answers foreground reads only between synchronous
`Observation.read` generator steps. A long notification or sidebar step therefore
blocks both foreground delivery and pipe readiness. The original UX profile is
`/home/ts/.cache/agent-scratch/ux-scroll-10604/observer.speedscope.json`: 599 main-thread
samples over 11.98 seconds, including idle time. It supports this scheduling defect,
not attribution of each previously observed 2.9-second foreground wait.

`MessageNotification.read_references` already batches references, and
`NotificationAssignment.capture` already captures one bounded SQL window. Do not
invent a per-message SQL repair. Generator steps may retain resource contexts;
`StoreLockRelease` has context-local lifetime state, so arbitrary per-step thread
hopping is not a safe ownership change.

Continue the source/consumer and resource-lifetime pass before implementation.
Keep one observer, canonical backend state, retained request identity and cancellation.
No frontend cache or timer. UX owns `ThreadReading` batching and observation teardown
in #946; this change must not duplicate those edits. Verification follows the coherent
implementation through the actual installed saved-history and held-paging journey.
