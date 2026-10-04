# Compaction native evidence — scoped Ready

Production is unchanged since `9660980c`; final qualification source checkpoint
`5639e72692539d1391628d44050ed49c75d15f55` adds only the installed control and
its original protocol bootstrap. Seven production files:68 lines deleted,
104 added. Three repeated construction/scheduling sites are replaced by the
existing compaction owner's acquired operation. No native artifact or schema
changes, and no public writes, provider input, restart or replay.

## One resource and its complete consumers

`OwnerCompactionCommit.open` owns the original native evidence descriptor and
closes it after the operation, including failure/cancellation. Adaptive ACP,
manual compaction and coordinated saved-owner preparation all enter this
scope. `CompactionBoundary.hold` lends it to existing
`NativeWitness.retained_task_facts` through `NativeEvidenceRead.borrow`.
The reader verifies the original byte prefix and only decodes new appends.
Capture, currentness, interrupted recovery, refusal, native commit and selected
admission therefore stop reopening and decoding the same entire JSONL.

Writer/owner/input/settings/current-source fences remain. Every native fact
observation still precedes global wire/BUS/registry/input acquisition. The
writer can append but cannot replace/rewrite the acquired prefix unnoticed.
No facts, proof or phase move into a cache/store. The original retained-fact
projection still runs at each held cut; this change removes repeated decoding,
not all branch projection or byte verification.

Preparation/currentness workers now use the existing joined
`Coordination.run_worker` lifetime. Selected refusal settlement and original
admission likewise join before the reader closes. The existing native commit
Future retains its cancellation/settlement lifetime. A cancelled capability
return grants no input: the original one-use admission cannot be recreated
from the journal. No input or turn disposition is changed by this resource.

The new optional `native_reader` on the constructor/boundary and the optional
reader argument on `NativeWitness` mean absence of a borrowed descriptor, not
absence of a lifecycle state. Short synchronous readers then acquire/close the
same existing reader through `borrow`; there is one decoder/verification
algorithm. All three production selected operations supply the acquired
resource. No compatibility reader, second catalog or source authority is added.

## Installed changed-path result

Reused `.artifacts/runtime-scoped-input534` with a normal wheel installation
and declared `[acp]` resolution;15 packages compatible.342 package members,
including the three declared force-includes, match source, wheel and installed
bytes. Native044 remains unchanged and fully pinned. Original567 wheel and
proofs remain retained; the owned sequential holder now contains568.

One actual installed native control passed in4.31s. The pinned native
SessionManager creates its real saved source; canonical private Comms protocol,
registry/turn and journal owners capture it. The new acquired bridge then
captures/rechecks, performs the actual inherited-FD native commit, and rechecks
the resulting append. Rewriting the certified prefix is refused even with
fresh file metadata; the original committed journal result stays unchanged and
the descriptor is closed. This is native/helper/state/custody acceptance, not
a configured provider latency comparison or physical UI.

The first invocation failed before the control: the existing native fixture
had not initialized its private protocol marker. Its raw failure is preserved;
the fixture now invokes `Comms.messaging.initialize_private_initial_protocol`,
without fabricated history or a production fallback. No assertion weakened.

The same installed acquired bridge also read the real retained SDK fork from
configured562:42,116,826 bytes/9,587 decoded entries. Initial acquisition and
decode completed at3.085077s; its next certified observation completed at
3.115201s (about0.030124s later). Source and `.input-proof` SHA256s remained
exact, and the descriptor closed. No child, provider or new input was launched.
These are two observations within one resource, not a turn latency A/B claim.

## Source evidence and remaining latency

NRA Package AST:311 production +362 tests +53 tools, zero parse omissions
before/after. It enumerates declarations/imports/writes/check references and
related consumers, including existing NativeEvidenceScope; aliases and dynamic
dispatch require the accompanying semantic read. Native JS is untouched;567's
native dependency census and the exact044 artifact remain applicable.

The original13.562s finish-to-next-prepare gap lacks per-read historical clocks.
This batch eliminates confirmed repeated work; it does not attribute or claim
to fix that entire gap. The98.141s selected-provider span remains distinct.
Original562/555 forks,79cb UNKNOWN,418 input and original560 ACP_UNCONFIRMED
are preserved. No repeat147s configured journey or unchanged controls.

Sch owns normal receiving after parent review. No native build, source-data
carry or runtime reset is needed. Scratch m568-native-evidence and02, the534
holder, original m562 source/proof and wheel/proofs remain protected until
receiving/receipt release. No active test process remains.
