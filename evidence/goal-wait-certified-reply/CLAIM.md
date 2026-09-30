# GoalWait certified reply ownership

Receiving owner: Mendel. Original assignment: #457 coordination-state workflow.
Base: ea8aa4eaee1ce3eed9aef19cd44010b592f83b26. Parent's frozen release
cohort and accepted #457/#461/#428 heads remain unchanged.

## Concrete open relation

GoalWait.matches resolves original Message.sender through the current registry.
GoalReplyScope also uses today's aliases for original recipient selection.
CapturedInputDependency.allows repeats that sender lookup. Original replies must
qualify by certified sender and addressed recipient incarnation, not a name reused
or renamed after publication. Original certified source owns the message facts;
GoalWait owns the captured dependency and after-sequence boundary.

Patterns: IDEN-6 (wrong identity join), IDEN-5 (identity authority split),
BOUND-2 (bypassing existing certified-delivery owner). No second semantic state,
index, seen cache, history facade, mirrored sender record or compatibility reader.

## Claimed consumer scope

- goal_waits.py: GoalWait.matches / GoalReplyScope and canonical wait semantics.
- goal_actions.py: dependency message/review/wakeup consumers, if the census confirms.
- turn_input_source.py: CapturedInputDependency reply eligibility; coordinate Arendt
  before edits to shared admission consumers in input_drain / ordinary_admission_rules.
- Existing certified source/query owner belongs to Sch; extensions requested directly.
- Native proof/lifecycle builder belongs to Arendt; no competing builder or operator.

## Acceptance to complete

Use original durable replies, same-name replacement and alias/rename changes to
prove correct original sender and addressed owner identity. Exercise real canonical
send -> standby -> reply -> wake/input admission -> consumed wait, plus excluded
wrong recipient/incarnation and UNKNOWN no-replay. Use existing retained native /
local-provider integration infrastructure for installed acceptance, no public
inputs, provider calls, schema edits or frozen cohort modification.

Status: source Ready with actual private installed native/ACP acceptance.
See READY.md for exact checkpoint, whole consumer closure and acceptance limits.
This has not been publicly installed or activated.
