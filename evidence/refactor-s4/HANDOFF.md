# S4: read evidence, response routing and presentation

Owner: S4. Worktree `/home/ts/wt/comms-refactor-s4-read-routing-20260927`.
Branch `codex/refactor-s4-read-routing-20260927`.
Base: A8/foundation `b97a108`; initial normal main merge `eca634a` (main `cf61da6`).
Publication revision and newest main integration are recorded below after commit.

## Delivered behavior and ownership

- ReadLedger is the only human read persistence owner, on A8 LockedStore/A2
  FieldCodec. The read fact is keyed by viewer creation identity and exact
  channel or DM participants `(name, created_at)`. It retains sparse displayed
  sequences because even a conversation maximum loses bounded-page/mention
  filtering evidence. Replacement bus inode invalidates prior wire read facts.
- A9 DisplayBasis, Conversation and DisplayedConversation are public exports.
  capture records the selected messages; through only subsets that evidence;
  mark_displayed performs a locked union. No view owns an advancing watermark.
  Human inbox/counts, channel display counts and acknowledgments use this owner.
  Explicit Mark Read selects the current complete view/inbox; it remains an
  explicit user command, distinct from automatic after-paint acknowledgment.
- DMDisplayBasis owns acknowledgment validation. It checks root/bus identity,
  selected human, participant creation identity and aliases. Unrelated registry
  writes and ordinary turn claims do not invalidate a real display. Old epoch
  observer attributes derive creation times for compatibility; no ownership or
  turn counter supplies DM identity. No S5 A7/counter or recovery mechanism was
  duplicated. The conservative older-unread contiguous-tail gate remains.
- Old scalar, view2 and transcript marker files cannot prove shown membership.
  A bounded exact-channel page reproduces this even for view2/exact (recorded
  expected-failure characterization). Therefore no positive legacy marker is
  unambiguous; migration resets those facts with a persistent visible notice.
  Executor delivery continues to use its old document, unchanged by human ACKs.
- TranscriptReadState retains only its disposable reply index and projection
  logic; its parallel JSON read/write/key implementation is gone. Transcript
  byte boundaries and bus message sequences are independent facts (native
  assistant output can exist without a bus row), stored by the same ledger.
  A transcript cursor still means the native transcript prefix; it is not
  reinterpreted as a bus display proof.
- ResponsePolicy uses DeclaredFamily. Direct, collective, mentioned-only and
  informational declarations own eligibility constraints, recipients, turn
  start, prompt guidance, batch separation and disposition keys. All five
  assigned consumer sites invoke that behavior. A test-only new policy reaches
  all consumers without new branches. No second family registry was created.
- BuiltinChannel owns aliases, canonicalization and exact stored-target
  admission. Channel catalog, audience manifests, message routing/history,
  broadcast and wake consumers derive these facts. The audited audience set
  meant rejecting aggregate/alias targets before freezing, not treating #any
  and broadcast as the same audience.
- SavedView/ViewPredicate serialize with FieldCodec; Message field declarations
  own wire aliases, optional omission and order. Existing wire key order is
  pinned because a private checkpoint fixture correctly detected its movement.
  FieldCodec's new metadata is opt-in; existing stores keep their formats.
- BusPresentation owns display metrics and caching outside MessageBus, with
  authority -> presentation imports prohibited by a test. The obsolete
  channel_display_activity implementation, ChannelReadScope, view-marker key
  construction, participant-marker encoding and DM marker writer are deleted.
  Bus's target-activity index decoder remains a wire-index projection; unrelated
  goal presentation classes remain with S8 in the current pre-C0 layout.

## Evidence

Use `/home/ts/.agent-comms/.venv/bin/python`, `PYTHONPATH=src`, pytest
`-o addopts='' -p no:cacheprovider --basetemp=<owned-root>` with outer `timeout 60`.
Shared Python lacked the already-declared metaclass-registry dependency. Tests
used a temporary, read-only source symlink to the existing local package;
no installation/download or shared environment change. It is removed after
checks. Recreate only in this worktree if reproducing with that same environment.
Tests needing fresh processes then exercise the same dependency source.

Final scoped logs (overlap is intentional; do not add them as unique test count):
- routing-final: 147 passed (routing, new declarations, channels, mentions,
  audience, wake, dispositions, FieldCodec).
- consumers-final: 63 passed (tools, relationships, replies, ACP disposition
  no-replay/process-exit tests, thread listing/order/scaling).
- reads-final: 59 passed (read scenarios, native transcript reads, LockedStore
  locking/concurrency and fault injection).
- last-read-policy: 57 passed after final DM validation/policy changes.
- envelopes-fixed: 100 passed, 8 optional stack tests skipped, including private
  envelope/checkpoint and paging behavior after preserving wire field order.
- Ruff passes all 15 touched production and 7 test files; scoped mypy passes the
  5 new/shared ownership modules. Diff whitespace check passes.

Characterization first: two strict expected failures against the old read
mechanism, then both pass after replacement. Additional tests cover hidden
messages in the same DM conversation newly exposed by any-mode, cross-view
consistency, peer/viewer delete/rebind, turn claims, real os._exit/reopen,
replacement bus, randomized page/mode/reopen sequences, new view predicates,
new policy dispatch, new aliases, golden wire order, migration notice and
one-way imports. Existing independent delivery/UNKNOWN tests remain intact.

Initial test harness failures are preserved: missing dependency in child
processes, an unguarded disposable pytest launcher under multiprocessing spawn,
and a registry-per-row performance regression. Launcher/environment fixed;
registry validation returned to one snapshot. The old field-order failure and
all pre-fix logs are retained in owned artifacts or earlier evidence logs.
No suppressed assertions, no live owner/provider calls, no recovery test edits.

## NRA, reasoning and proof boundaries

NRA checkout `/home/ts/code/projects/nominal-refactor-advisor` was used directly,
not an installed substitute. Read its CLI/API docs, architecture playbook,
batching guidance, and the referenced paper at
`papers/docs/papers/paper1_typing_discipline/markdown/paper1_jsait.md` (sections on
same-looking meanings, retained identity and implementation reach). The paper's
confusability collision applies to omitted-vs-shown messages below a maximum.

Initial default 20-second scan was incomplete. Retried with single parse/analysis
workers, 150-second internal budget and 165-second outer timeout. Both the
baseline package snapshot and final entire package complete all 79 detectors,
0 omitted, exact_compact_global. Findings 28 -> 23. Six assigned ownership
labels disappear: ResponsePolicy, ThreadRole, ControlClassification,
MessageAudience, SavedView and ViewPredicate. No findings target new S4 modules.
See nra-summary.json; full raw reports remain under .artifacts/s4.

The remaining Message mirror is importing.py:48, ImportRole.pi_message: a
provider-native message payload, not the inter-thread Message wire format.
Those independent semantic owners must not be unified merely because fields
share names. DisplayOrder composes thread/channel orders whose key functions
operate on distinct domain values; no mirrored global ordering owner was added.
Other remaining reports concern unassigned recovery/goal/export boundaries.

This is authored semantic implementation, not a claimed native equivalence proof
or automatic NRA codemod application. The read behavior intentionally changes;
NRA is coverage/ownership evidence, and the local tests establish the stated
behavior boundaries. No full suite, installed wheel, live owner UI, CI or deployment is claimed.
Mounted Toad test-app paint/rebind validation is recorded below. Owner requested bounded local shards
and no CI wait. Artifacts are preserved per crash-recovery instruction.

## Integration and remaining work

See DISPATCH.md for A9/Toad API details and three narrow, value-preserving ACP/
parent alias/role consumer follow-ups. S8 goals were not modified. The latest owner directive assigned mounted
Toad validation and its focused fix to S4 in a separate persistent worktree.
Parent must merge foundation/A8 once, merge S4/S8 and the Toad integration
normally, refresh pins and decide activation. No current live
read files were touched. S5 can later replace participant tuples with its shared
identity declaration; do not add a second incarnation or owner-epoch counter.

Sparse read membership has storage proportional to acknowledged wire messages;
this favors correctness over a lossy watermark. A later compaction must retain
holes, not replace sparse membership by a maximum. Renames may conservatively
make earlier incarnation-keyed DM reads unread under the approved name/creation
fallback; they cannot transfer read evidence to a rebound peer.

## Publication checkpoint

Implementation commit `4aa1371`. Normal newest-main merge `f75dc81` incorporates
main `bf402f5` without conflicts. Merged-tree focused shard: **115 passed in
42.42 seconds**, recorded in merged-tests.txt. A fresh merged package NRA scan
is complete (79 detectors, none omitted); exact count in nra-summary.json.
AST comparison against eca634a preserves all five named S8 goal/presentation
declarations and all 13 Comms goal methods. Protected parent files match
origin/main byte-for-byte in git diff. No additional parent-code edits.

Publishing this implementation as a draft to OpenHCSDev/agent-comms. Final URL
and verified remote SHA are recorded in the final checkpoint once created.

## Mounted integration closure (2026-09-27)

Core draft https://github.com/OpenHCSDev/agent-comms/pull/137 was published at
b3acdf2, after the interruption was resolved by inspecting both Git and remote.
The previous push of 40c5599 had succeeded; no PR had been created. Normal merge
b3acdf2 incorporated main b0d4900/PR136, preserving restore_stopped/restore_missing.
The focused restoration/read/policy check passed 25 cases.

Mounted Toad at published main 0894005 in a new owned persistent clone revealed
that its old after/expanded_after gate prevented any bounded tail acknowledgment.
A two-row painted-tail pilot failed against the archived original source and
passes with the integration. The follow/refocus pilot additionally exposed the
all-fetched-rows-visible gate. Toad now retains bounded pending page proofs,
acknowledges their painted subsets, and keeps other rows pending for scrolling.
Core DisplayBasis.select intersects with captured membership; it cannot add a
sequence from outside the page. ChannelDisplayScope.same_projection compares
inclusion semantics, not read progress or unrelated store writes. DM retains its
conservative inbound/older-unread gate and also subsets its proof to painted rows.

The core's randomized shown-only test now varies painted subsets as well as page,
mode, prefix bound and reopen. A captured page can acknowledge later subsets
across prior acknowledgments and unrelated registry updates; changed participant,
viewer or bus identities still invalidate it. Final focused core shard: 54 passed
in 4.33s. Complete package NRA remains 79/79, 0 omitted, 23 findings. Logs and
summaries retained here; raw scan/test artifacts remain in .artifacts/s4.

Toad source/worktree: /home/ts/wt/toad-s4-read-routing-20260927,
branch codex/s4-mounted-read-basis-20260927. Eight mounted pilots pass: DM rebind,
bounded displayed-only page, partial viewport plus scroll, any-mode scope
expansion, any-mode menu, follow/refocus, migration notice and divider-only
visibility. Four history-reader cases pass (including channel and DM bus inode
replacement, turn-claim identity stability and background admission). The cache
identity now includes bus/viewer creation identity and has no epoch comparisons.
Full exact source and dependency recipe is in that tree's evidence/s4/HANDOFF.md.

Tests used pre-existing Python 3.14 UI dependencies and Textual 4fa6a9c with core
and Toad source mounted through PYTHONPATH. No installs, live provider calls,
owner restarts, deployment, shared-root edits, native runtime or recovery edits.
The migration fixture now creates a pre-ledger root before reopening; writing a
legacy marker after migration is intentionally not a second migration. The
viewport fixture gates initial automatic ACK while arranging divider-only
geometry, since the initial body paint is now legitimately acknowledged. The
follow pilot waits for committed layout after async participant hydration.
Final publication revisions and the Toad PR are recorded in CHECKPOINT.md.
