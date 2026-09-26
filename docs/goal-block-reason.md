# Blocked goals carry an explicit, inspectable reason

A goal can be blocked only with a bounded, nonempty `block_reason` on the goal
row itself. The reason is a separate field from `progress`: progress keeps the
running narrative, while the reason states exactly what input or decision the
goal is waiting for.

## Contract

- **Every new blocked transition requires a reason.** `Comms.update_goal`
  rejects a blank or missing reason for `action="blocked"` before any registry
  or history write, so a failed call leaves the goal and journal untouched.
  The model tool `comms_goal` uses its `progress` argument as the reason when
  blocking; separate internal APIs accept an explicit `block_reason`.
- **Reasons are bounded.** At most 1024 characters after stripping; blank
  whitespace is rejected, never silently defaulted.
- **Prior progress is never recycled as a reason.** Blocking with no reason of
  its own fails; it does not copy a stale progress report that may describe
  already-completed work.
- **Automatic blocks record their own diagnostic.** `block_goal_after_failed_turn`
  appends the exact diagnostic to prior progress and stores it as the reason;
  `block_unverified_goal_completion` **replaces** progress with the diagnostic
  and stores it as the reason. In both cases the visible explanation matches
  the automated failure rather than older text.
- **Owner resume refusal is persisted.** When an owner Resume hits a private
  attempt that is still `blocked`, the refusal explanation ("The interrupted
  goal attempt is unresolved …") is written to the goal as its `block_reason`
  before the `ValueError` is raised. A later reload therefore shows the real
  explanation, never an invented one, and prior progress is retained.
- **Legacy rows are labeled, not inferred.** Blocked goals written before this
  field existed have `block_reason=None`; presentations show
  `Blocked · reason unavailable` instead of pretending the old progress was
  the reason.
- **Retry clears the reason.** An explicit human Retry (including the ACP
  direct replace) resets `block_reason` to `None` when the goal becomes
  active again.

## Visibility

`Goal.block_reason` round-trips through the registry and goal history. The
execution projection (`goal_snapshot`, `goal_execution`, thread views) exposes
it as `GoalExecution.block_reason`, and the presentation renders
`Blocked · <reason>` up to 160 characters (older rows: `reason unavailable`).