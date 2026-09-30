# C4 current ownership scope

Measured at current main697bba42 before production continuation.
HistoryViews is478 lines and already below the500-line plan threshold; do not
extract more components from it merely because its historical receipt said1012.
CommsAgent remains549 lines. WireLog is excluded by the original C4 decision.

The remaining state-owning responsibility is ACP native-cursor observation and
publication: scope capture, projection revision, last announced observation,
nonblocking refresh and publication to the attached runtime. Its state currently
lives in CommsAgent as two session-keyed dictionaries. The native input/cursor
proof remains owned by NativeSourceCursor/CursorOwner and the durable ledger.
A transport presentation component must consume those original proof owners,
never become another admission authority or source cache.

Before implementation, trace every effects/session/drain/compaction consumer.
Close all direct private dictionary accesses and old helper callers when moving
the responsibility. Use the existing CursorEnvelope/observation family. Do not
carve mixins, duplicate private launch pins or keep compatibility helper aliases.
State ownership and a documented reduction in edits for an additional transport
consumer must accompany class-size closure. Mere relocation is not completion.

The parent owns this draft. Arendt owns complete turn lifecycle425/211,
Schrodinger owns ThreadPresentation identity binding426/210, and existing
shared-file owners must coordinate directly before overlapping edits.

## Working implementation checkpoint

Current main7995bc5a is integrated normally. CommsAgent now owns420 lines
(before formatting), down from549. HistoryViews remains478 and is not carved up.
The connection-owned CursorPublication reads the original NativeSourceCursor
and publishes the existing CursorEnvelope. A CursorDelivery holds only local
transport revision and last successful publication for its one session: these
are delivery bookkeeping, not another native proof, input or message store.
Its public predicates own equality and refresh policy; two parallel session
maps and six CommsAgent helper/abstract entrypoints are deleted with all callers.
SessionLifecycle derives queue metadata from InputDrain and cursor metadata
from the publication owner. InputDrain refreshes the same publication owner.
No compatibility methods or retained old helper names remain.

Adding a transport observer no longer requires a CommsAgent subclass or its
turn/input effects: it consumes CursorPublication with the original Comms and
session-update transport. The underlying native proof and observation family
still own evidence and variants. This is responsibility/state closure, not a
claim that merely moving observation code is polymorphic factoring.

Verification so far: 24 focused cases passed, including real TCP publication
and real flock contention/recovery. One mocked selected-send assertion fails
because covered_seq is2 rather than original seq1; the identical committed
current-main assertion fails in1.75s. This suite is not reported green.
The existing source ownership guard passes2 cases in0.65s and now prevents
retired cursor helpers and dictionaries from returning. Static undefined/import
checks pass. The initial attempt removed a NativeSourceCursor import still
needed by input advancement; it was restored before the final24/1 result.

An installed artifact and actual source-projection attachment remain required
before readiness or merge. No default package or live owner was changed here.
The staged Toad208+202 environment likewise remains inactive while its concrete
late reader-jump failure is corrected by its owners.

## Installed and staged acceptance

Noneditable Corededa0dec artifact imports resolve exclusively to site-packages.
The existing real TCP/socket and flock-contention recovery journey passed from
that installed artifact:1 passed in2.44s, no new input or replay. Actual staged
`toad-comms agent-comms-ux` on the unchanged active route completed session/load,
painted32 saved events, and emitted no load error. Exact log:
`/home/ts/.local/state/toad/logs/Agent_Comms_2026-09-29T19_50_04_949966.txt`.
The test-owned Toad/ACP processes ended normally; default launchers were not
changed. This is actual staged attachment, not a claim that the default workers
already run this code or that unrelated queue/reader bugs are resolved.

Committed package ratchet against7995bc5a: zero increased measures. Production
lines deleted:150. Added:167, including155 in the state-owning publication
component. CommsAgent420 / HistoryViews478 both satisfy the original500-line
threshold. Two transport maps and all retired helper/caller names are removed;
the original durable native proof, cursor ledger and invocation authority remain
unchanged. No runtime-store format changes originate in this C4 replacement.

Owned exact baseline extraction removed after saving the failure evidence:
10,343,888bytes. Installed acceptance env remains parent-owned until activation
or retirement; its purpose is the staged C4 artifact and source projection.
