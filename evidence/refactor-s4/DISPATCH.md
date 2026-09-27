# S4 integration note, 2026-09-27 America/Toronto

Owner: S4, /home/ts/wt/comms-refactor-s4-read-routing-20260927,
codex/refactor-s4-read-routing-20260927. No edits to other worker trees.

S8: shared-file changes are S4 methods, imports and types only. Goal classes,
goal methods and comms_goal are untouched. FieldCodec gains declaration metadata
wire_order and wire_omit_default plus sorted frozenset encoding/decoding; defaults
preserve existing behavior. Merge normally; do not transplant dirty source.

Parent/S8 ACP integration (narrow, not required for changed read semantics):
- acp.py GLOBAL_TARGET's literal should derive from BuiltinChannel.ALL.value.
  S4 deliberately leaves ACP to S8 and does not copy S1's published files.
- claim_admission.py's global target literal should derive from the same member.
- coordination_response.py _require_live_registry_owner can query
  thread.role.executable instead of comparing ThreadRole.AGENT. No change to
  parent recovery methods is included here. These cosmetic remaining literal/
  predicate consumers retain their existing values and behavior.

A9 published at agent_comms.DisplayBasis (implementation read_basis.py), with
Conversation and DisplayedConversation. ReadLedger.capture builds it from the
actual selected messages and the captured registry; through(n) subsets those
messages, not the wire prefix. mark_displayed(viewer, basis) merges sparse sets
under LockedStore. No parallel A7 or recovery fence was introduced.

Toad integration: existing channel_display_page -> display_scope ->
mark_channel_view_read(expected_scope=..., through=...) and dm_display_page ->
display_basis -> mark_dm_view_read(expected_display_basis=..., through=...)
call signatures are preserved. ChannelDisplayScope carries .displayed; retain the
returned evidence after paint (or select its painted subset via DisplayBasis.select). Do not reconstruct scopes from channel
names/watermarks or invent markers. Legacy after/expanded_after fields remain
zero-valued compatibility data, never read authority. DM viewer_epoch/peer_epoch
observer attributes remain available as derived creation-time aliases (floats),
not ownership/turn counters; new code should use viewer_created_at,
peer_created_at and displayed. Full registry or marker revisions are not identity
proofs. Server validates DM incarnations, aliases, root and bus before marking.

Mounted Toad validation is now complete in the own persistent tree
/home/ts/wt/toad-s4-read-routing-20260927, branch
codex/s4-mounted-read-basis-20260927. The focused integration removes obsolete
watermark/all-page gates and keeps captured per-page evidence for painted subsets.
DisplayBasis.select(sequences) retains only original proof members. Channel
projection matching ignores read progress; identity validation remains required.
Toad derives DM/cache identity from creation time and bus inode, never owner epochs.
The parent owns pin refresh and deployment; see HANDOFF.md and CHECKPOINT.md for
passing mounted cases, exact revisions and PR links.
The current conservative DM contiguous-tail/older_unread check remains compatible.
Explicit Mark Read retains its user-command meaning: it selects the current
whole inbox/view without asserting that a UI painted it. Automatic painted-page
ACKs mark only the messages in their basis. Neither action alters executor
AcpDeliveryCursors or replays UNKNOWN inputs.
