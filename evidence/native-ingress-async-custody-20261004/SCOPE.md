# Native ingress custody

Original #633 installed03 proves one process holding exclusive wire custody while
its event-loop thread waits for shared custody on that same inode. The original
private controller was interrupted through ProcessIdentity/Platform and retired
through its original cleanup. Started/reserved inputs and source proofs are kept;
there is no replay or successful ACP resolution claim.

Existing MaintenanceBarrier.admit_ingress_async owns asynchronous shared ingress.
Migrate the original backend send boundary and every initial, queued and interrupt
consumer through it. Keep named admission checks, durable bindings and synchronous
stdin.write under that same acquired source; release before pipe drain/native ACK.
Delete OwnedSendAdmission's competing acquisition and magic forwarding marker.
No new lock owner, reentrant registry, cache, retry, deadline, native artifact or
wire/schema change. Private selected admission retains its existing separate
joined raw-writer ownership. #637 cohort/schema/wake context is disjoint.

Source and complete consumer migration first; one affected installed native
follow-up/ACK/STARTED/UNKNOWN and maintenance/cancel batch after source review.
No new environment or provider; reuse only an explicitly granted existing holder.
