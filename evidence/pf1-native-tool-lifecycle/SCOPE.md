# PF1 per-native-tool-call lifecycle

Base: main c895ff0 (PR212 PATH fix included). Owner: this PF1 worktree; Pascal
owns PF5. Parent owns activity/assignment presentation, integration and activation.

## Determining owners and complete migration

- `NativeToolCall` owns one process-local call's observation, owner admission,
  correlation, wait, completion and cancellation. Independent observation and
  admission classes implement their valid transitions and shared rejection.
  The per-call Event only wakes waiters; it never authorizes execution/completion.
- Existing `CodingCall` and `SelectedToolRequest` inherit that behavior and own
  policy-specific terminal semantics. No extra payload/transaction wrapper or
  second selected slot store. Selected field projection derives from its own
  dataclass declaration; Pi retains the normal coding arguments schema.
- Existing `OwnerToolSocket[Call]` owns authenticated transport, typed decode hooks,
  its call index and client cleanup. Native event callbacks select that exact
  call; the owner callback remains the sole mutation/admission authority.
- `CodingToolOwner`, resource claims, NativeToolMode, selected durable slot and
  terminal functions keep their existing authority. Transport tokens and observed
  starts cannot mint a claim, native input or admission generation.

Production write set: native_tool_call.py (new), channel_coding_tools.py,
selected_tool_broker.py, native_pi.py (rejection-type import only).
Current tests: channel coding, selected broker, new cross-policy lifecycle suite.
No paired Toad production API consumer identified. No coordinated_runtime,
HistoryViews, assignment presentation, native package or owner launch edits.

## Deleted surfaces

Coding socket `announced`, `started`, `finished`, `admitted`, `denied`, shared
`changed` and reconstructed membership-completion logic. Selected socket
`_approved`, `_approval_changed`, `_announced_id`, `_announced_args`, `_started`,
`_finished`, `completed_call_id`, and bypass `approve_tool_start`. Duplicated
announce/start/terminal/completeness implementations migrate to the actual shared
owner. `parse_selected_request` becomes the request's single boundary; all callers
migrate, no free-function alias. Consumers import SelectedToolDenied directly
from its new lifecycle owner.

## Behavior strengthened within the assigned surface

Concurrent duplicate requests consume once. Socket-before-event waits on the
specific call, including interleaving. Cancellation/timeout leaves uncertain
state and cannot later accept a new start or retry. Failed terminal fsync cannot
be converted to completion by a second terminal event. Closing the listener
cancels/drains accepted clients BEFORE waiting for server closure (required by
current Python's wait_closed semantics); late handlers refuse a closed owner.
Normal denied tools may finish with an error as before. Selected proof still
requires successful owner action and the matching durable terminal; a visible
receipt alone cannot promote failure to success. No automatic UNKNOWN replay.

## Method and evidence limits

Read NRA skill/current owner decisions and full PF1 audit; inspected complete
native callbacks/current callers. Before/after NRA scans use full package context,
selected reporting files, 79 detectors, zero omissions, exact_compact_global.
Manual authored declaration/state migration: no NRA DSL-equivalence certificate
is claimed. Initial scan had zero findings despite the manually established
lifecycle split; counts are not completion evidence. The intermediate after scan
identified two selected argument-shape mirrors. Those now derive from the request
fields instead of retaining duplicate key lists.
