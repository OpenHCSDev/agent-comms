# Integration and live completion checklist

Owner: parent Codex. Active goal set by Tristan on 2026-09-28. CI deferred;
local focused checks plus the actual installed affected path are the gate.
No feature is complete merely because a draft exists or a PR was merged.

## Current shipping batch

- [x] Comms272: nominal compaction failure/recovery, typed input states, codec
  ownership. Merged main fdf3c917. 134 local checks, ten installed native checks,
  actual installed ACP/selected-provider fork compaction and reply passed.
- [x] Comms271 reconciled and closed: all its implementation is carried by272.
- [x] Combine committed T2 core262 and T5 core270 with272 in parent's integration
  branch. Keep FieldCodec as sole codec; keep typed snapshots unconditional.
  Eighteen family/transcript/codec integration checks pass.
- [x] Dalton: finish T2 core262/Toad122 caller/request/deletion/actual-path closure,
  update on merged272; return complete published heads to parent and Carver.
  Parent has integrated core85363055 (declared requests, current callers,
  declaration-owned display errors with structured input status). All35 focused
  checks pass; aggregate coverage threshold alone returned exit1. Dalton is
  finishing actual-path receipts and related caller corrections.
- [x] Carver: integrate Toad120/121/123/124 in own persistent integration tree;
  resolve overlapping declarations with T2, test installed UI/native behavior.
- [x] Parent: integrate final T2 and paired core270, review and merge the completed
  Toad batch with current core/native/Textual pins. Close superseded53 only after
  its discovery behavior is verified in merged120.
- [x] Parent: stage final immutable runtime, verify actual isolated installed
  owner/ACP/UI/bus path, then quiet live cutover and fresh attachment checks.
  Core66e8fa13/Toad67ddc9e6/Textual16ede007/native5fde now LIVE. Actual installed
  native queue/DM/channel/reopen passes. Four fresh live ACP attachments pass;
  three mounted live channel/DM views pass. Only the compaction journal and
  input-disposition runtime stores reset; executed operator deleted.
- [ ] Parent: verify compaction/recovery, readable error/log views, channel
  participation feedback, commands/goals/history/rendering in the live pair.
  User confirms live compaction completed in about2:40 and a later prompt worked.
  One prompt encountered provider WebSocket1011; explicit user retry succeeded.
  Dalton owns clearer typed provider-error feedback. No automatic input replay.
- [ ] Parent: reconcile source PRs, publish actual live state and cleanup artifacts.

## Parallel remaining work

- [ ] Wegener: replace obsolete fake-owner native integration fixture with actual
  pinned native host path and prove ordinary/private one-original admission,
  correction/queued input/refusal without replay. Own test/host files only.
- [ ] Tesla: establish116 ownership first, then finish an independent substantive
  workspace/resource implementation slice (or take over only if unowned). Use110
  measurements, preserve operational ACP/editor state, coordinate with Carver.
  Completed independent slice126: demand-built session panels, installed64-tab
  and actual terminal checks, editor/undo/shell continuity. Continues116 global
  presentation bounds and actual ACP retention; original claim remains recorded.
- [ ] Noether: establish50 ownership, finish authenticated loopback browser path
  in own worktree and verify actual serving/authentication. No external exposure.
- [ ] After T2/T3/T5/T6 merge, remeasure and finish T4 as specified: nominal
  turn owner, block navigation, TabOrder and clipboard families, no mixin carving.
  Carver now owns Conversation/blocks/Agent lifecycle and the observed Question
  disconnect/mount race. Parent owns App TabOrder/clipboard; Tesla owns116.
- [ ] Parent: finish actual remaining original/round-two plan acceptance and
  deletion audit against current source; stale reports are not current blockers
  and do not establish completion. Keep explicit requirement/evidence mapping.

## Quiet runtime cutover

Reset only the declared current-format runtime stores: compaction-commits.sqlite3
and its SQLite sidecars, input_dispositions.json, plus any runtime stores explicitly
changed by final T2. No old-format loader/converter is introduced. Preserve native
sessions/input proofs, durable wire and goal history, owner decisions and durable
routing annotations. No old UNKNOWN or queued input is replayed. Exclude old ACP
writers, require no active turn/compaction, stop/restart exact process identities,
and reopen admissions with the new pinned pair. Delete any executed one-shot
operator from tools/cutover after installation, retaining a concise result receipt.

Worktrees stay under ~/wt; shared main and other agents' trees remain untouched.
Use bounded tests, stop completed test processes, clean owned disposable copies,
and monitor disk/RAM. Latest check: root8.9GB free, home27GB free, RAM8GB available.

2026-09-28 cleanup: verified the original isolated live-provider test owner dead
and removed its143792851-byte fork plus544-byte proof. Original user session and
the small result receipt are retained. Latest resource check: root8.9GB free,
home26GB free, RAM9.8GB available.
