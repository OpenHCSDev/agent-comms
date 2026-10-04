# Installed qualification remains incomplete

The three original serial runs are retained without alteration. Run01 failed
collection at a retired turn-update import. Run02 exposed saved-owner declaration
without canonical participant restoration. Both source consumers are corrected
at their existing owners in the published fixture checkpoint.

Run03 reached actual saved SDK/native turns: six controls passed, nine obsolete
failure/observation assertions failed, and the queued follow-up exposed a real
production custody deadlock. The controller held the exclusive wire lock while
its event-loop thread waited for shared custody on the same inode. Kernel lock,
identity and exact guarded interrupt receipts are retained. Two exact-controller
SIGINTs allowed original asyncio/test teardown; the controller and all process
references to this private output are absent. No group/public signal, input replay,
lease clearing or successful ACP disposition is asserted. Original Started and
reserved rows, native saved history and proof files remain in the private root.

Production cause: InputDrain.queue_followup owns asynchronous exclusive custody
through joined reservation work. Concurrent native prompt/steering obtains the
shared MaintenanceBarrier synchronously, blocking the loop needed to finish
reservation and release exclusive custody. This needs a complete native-send
boundary correction, rather than a timing change in the fixture.

Qualification is not Ready. A separate source checkpoint will migrate initial,
queued and interrupt admission through the existing asynchronous ingress owner.
The fixture batch also needs original typed PromptFailureReceipt observations
and acquired owner-socket subscriptions, not raw obsolete exception contracts.
