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
