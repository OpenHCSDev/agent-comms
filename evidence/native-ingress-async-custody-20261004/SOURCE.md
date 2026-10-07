# One native ingress owner

Original #633 run03: same PID155773 held exclusive wire inode3841569 while its
main loop waited for shared custody. InputDrain.queue_followup retains exclusive
custody across joined reservation/rollback work. A concurrent synchronous native
send blocked the loop needed to receive that result and release the exclusive
resource. Original kernel/identity/exit evidence remains on #633 d4e0ad4f.

MaintenanceBarrier.admit_ingress_async now acquires the original shared resource
once for backend initial prompt, InputForwarding.send_prompt and interrupt.
Delegates retain exactly their original registry/goal/input/journal/source checks,
durable binding and synchronous stdin.write inside that resource; drain/ACK and
provider work happen after release. Cancellation during acquisition never enters
the delegate or writes bytes. There is no reentrant registry/cache, timeout or
retry. Original selected PrivateSendAdmission/TrackedTurnSession raw-worker
custody remains unchanged.

OwnedSendAdmission now consumes that acquired ingress instead of independently
acquiring it. The _maintenance_wire_locked marker and getattr dispatch are gone.
NativeStartupAdmission retains its original root; its directory is derived from
that root. All three sends use that same startup root, deleting the backend/input
copies of environment/fallback selection. Thread.native_environment originally
supplies the Comms root for owned turns; explicit selected startup retains its
original root. No root identity is inferred from directory positions.

Original Package.load parses all production and test roots with zero omissions.
Before/after declarations/imports/calls are retained. Custom synchronous delegate
callbacks are a dynamic boundary, not proved by AST. All production callers and
two direct maintenance controls are migrated. The added physical-lock control
checks that an exclusive async owner can resume and release while shared send
waits; the existing maintenance control still prevents bytes through closed
admission and waits through the final synchronous write.

Final affected installed qualification is committed in #633 at 1a05c7e9,
evidence/native-source-turn-publication-20261004/. Production here is byte-equal
to that normally joined #638+#640+merged #637 source; this does not claim an
old standalone package qualification. The source owner batch deletes 85 lines
and adds 71 across four production files.

Original physical shared/exclusive ingress, exclusive final-write, interrupt,
cancellation isolation and hard-exit UNKNOWN controls passed in the preserved
#638 original 11-pass/16-fail result. Those accepted controls were not repeated.
Changed source640 installed01 passed native queued ACK/start and four retry
paths/exact source/replacement fences (16 pass/4 fixture fail). Original02
remains 2 pass/2 fixture fail; final03 only two corrected cases passed, exit0,
16.623298s. Original FULL wire publication, native context receipt, distinct
retry lease/goal pause/unchanged UNKNOWN and all waiter/inbox/child cleanup pass.
The previous mixed receipts remain mixed; no failure is relabelled or replayed.

Normal wheel58a11a61 has 347 source/wheel/installed members exact, 84 unchanged
environment/dependency keeper files. Actual localhost HTTP/native/ACP scope,
nonzero native inputs, external/paid/public/replay zero. Exact recorded child
and controller identities/groups absent at joined terminal; Bohr independently
closed whole540 purpose with 253 all-UID processes/zero borrowers/zero gaps.
Immutable086 read/execution returned. No timing or public-channel claim.
Normal merge #638 then #640 then fixture #633; parent owns publication.

## Correlated native preflight, 2026-10-07

Original live failure diagnostic:
/var/tmp/agent-comms-live-20260927-wzjtqhza/diagnostics/cc5fd116d4b74b50811b4fa8d0533d5c.json.
It attests NotSent during native preparation, records the original get_state
command and empty stderr, but does not retain the unexpected event. Its exact
type cannot be reconstructed. Neither that input nor the manual resend is run
again by this change.

The concrete source defect is requiring the next channel event to be the state
reply. Selected immutable Pi rpc-mode.js produces unsolicited extension UI
notifications/status/error events and binds extensions before serving commands.
NativeQuery.exchange already correlates multiplexed replies via PendingRequests.
Both streaming and tracked preflight instead assumed the next event matched.

PiEvent/Response now own the nominal command/id response relation, also consumed
by require_request. PendingAttestation changes state only for its own matching
successful response, with the unchanged capability/payload/expected-session
checks. Its generic observe no longer independently grants attestation from an
unrelated GetState reply. Streaming and tracked consumers continue ordinary
event processing until this response. AgentSettled owns the shared distinction
that prior completion while attestation is pending cannot settle this new turn;
both event consumers use it. The watchdog keeps the original absolute startup
deadline ahead of optional stats waiting. No timeout is raised or renewed.
The current installed initialization pilot uses existing NativeQuery.exchange
instead of assuming its next receive is the reply. Frozen evidence is unchanged.
Native custody compaction reattestation/catalog discovery already use exchange
and need no alternate receiver. No message_bus.py/turn_context.py edit.

Complete source traversal parsed/compiled 759 src/tests/tools modules without
imports or omissions; dynamic external consumers remain unresolved. Nine
focused checks passed: pending promotion, unsolicited status/old settlement and
foreign response followed by exact reply, plus original wrong-ID/EOF/invalid
data, missing capability and no matching preflight-before-user-start refusals.
These are existing authored subprocess protocol controls, not native SDK/live
qualification. The positive fixture initially lacked the required assistant
content array, reached that later strict decoder and failed; only its fixture
was corrected and that check rerun. Eight other passed checks were not repeated.
No public start/stop/restart, SDK/provider operation or failed-input replay.
Parent must integrate/build/deliver before claiming the live failure is repaired.
