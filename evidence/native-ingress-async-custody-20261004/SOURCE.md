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

Changed Python sources compile; no test/provider/environment was launched here.
Actual native followup/ACK/STARTED/UNKNOWN + interrupt/cancel qualification requires
an explicit successor package purpose on the existing released holder. #633 is
still unqualified; its remaining obsolete typed-error/subscriber observations
are fixture consumers, not justification to bypass this actual custody failure.
