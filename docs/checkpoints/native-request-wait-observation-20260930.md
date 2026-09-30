# Native request wait ownership

Owner: Arendt. Parent retains public activation. Base: 29bbe95d.

This continuation owns the installed native request journey from dispatch through
transport acceptance, first and later stream events, awaited native callbacks,
journal append and ACP publication. Historical 129.898-second evidence cannot
allocate the wait to the provider: it includes awaited local callbacks and lacks
dispatch, header and first-delta timestamps.

Mendel remains sole writer of PR453 native provider adapters and budget policy.
Request extensions there rather than implementing another adapter. Preserve the
existing diagnostic and NativePhase owners, original request/input/turn and owner
incarnation, accepted-stream no-replay semantics and valid unbounded model work.

Acceptance: controlled localhost provider delay versus blocked local publication
through the actual native/ACP path. If necessary, one short configured Sol/Off
call in an owned private fork with representative original history; no public
input, paid alternate model, parent settings changes or historical UNKNOWN replay.
Provider capacity is reported only when the transport supplies that evidence.

No new lifecycle store, retry owner, status mirror, timer or certificate cache.
Source and installed results will be recorded at their actual strength here.

## Concrete local publication defect and checkpoint

The original `SocketClient.session_update` awaited an observer's `drain()`.
The owner broadcast awaited each observer serially. Installed baseline05 proved
that one real non-reading Unix subscriber held an ACP publication for 1.029s
and an awaited native callback for 2.564s although the controlled provider wrote
its complete response in 15ms. This does not attribute the historical 129s.

The existing SocketClient now owns all its output handoffs: updates, permission
requests, result/ready records and error envelopes. It writes into the original
asyncio transport, yields to readers, and retires only that socket if its buffered
bytes exceed the transport's high-water budget. The budget remains the actual
transport's configuration, including a caller's parameterized override. One
original saved snapshot's record size establishes the minimum byte budget for
that record. There is no second queue, whole-turn timer, copied transcript,
input disposition, or retry authority. Existing disconnection denies pending
permission futures and does not cancel the original turn.

Installed07 passed through actual Toad input clicks, ACP, worker and native Pi:
two originals, two localhost requests, both replies painted; a non-reading
observer retired while the controller, another fast observer and a temporarily
busy observer retained their attachments. Fresh recovery read the 392391-byte
canonical snapshot and matched its original content/frontier identity. Recovery
did not send another input. [Sanitized receipt](../../evidence/native-request-wait/installed07.json).
The busy-reader fixture is being tightened to resume after actual streamed text,
rather than after the provider finishes writing. This final affected check
remains open, together with Mendel's transport hooks.

Run06 is a retained failed attempt: its premature complete flag is not acceptance.
The first watermark correction retired a healthy fresh reader of one large saved
snapshot; this caused the original-record byte-budget correction. All private
failed traces, journals and proofs are retained. Fixture daemon teardown was
also corrected to use the existing exact test-owner lifecycle; idle05/06/07
daemons were stopped through OwnerLifecycle after recording the receipt.

Thirteen focused phase, subscription and unsent-attachment controls pass. Native
request timing is attached to the original native ProcessIdentity and exact
turn/owner fence. Python append clocks are named recorded times, not ingress
times. Completed publication counters describe preceding completed operations;
they cannot be subtracted from native clocks without an original clock bracket.
Optional diagnostic I/O failure reports a warning and cannot change admission.

Remaining: Mendel's true dispatch/header/raw-first-event/terminal adapter hooks
against his budget owner; final coherent native manifest and installed affected
journey. Original retry attempts come from `maxRetries - retriesRemaining`,
not a second attempt counter. Accepted streams are never replayed.

`ModelWaitPhase.source` changes the live phase publication ABI. Release requires
matched owners and clients through the canonical idle batch; durable formats,
native journals, reservations, UNKNOWN receipts and original proof stores are
unchanged. Parent owns that cutover. This checkpoint is pushed, not Ready/live.
