# Live notification window followthrough

The user requested a second profile while sending a message in #openhcs. Parent sent no message and did not inject UI input or change the running installation.

## Observation

The original running Toad process was profiled for 120 seconds at 100 Hz with nonblocking py-spy, threads and full filenames. Raw capture is retained at `/home/ts/.cache/agent-scratch/parent-live-burst-20261006/openhcs-live-burst02-speedscope.json`.

The sampler returned 6606 samples and 5262 read errors. The raw JSON contains invalid UTF-8 bytes; analysis decoded those with replacement, preserving the original bytes. Counts below are inclusive observations, not CPU percentages or frame-time measurements.

- HistoryViews.message_notifications: 1988 observations.
- FieldCodec.decode: 2427 observations.
- Main-thread root_is_current: 314 observations; CommsChat._refresh: 181.
- Message.message_id was a sampled leaf 239 times; NotificationAssignment receipt matching was another repeated leaf.

The current original NotificationAssignment implementation is identical to the installed implementation. Its per-delivery recipient loop scans all acquired assignment rows and computes original.message.reference inside that scan. This repeatedly hashes the body for each candidate row and recipient.

## Change

NotificationAssignment now matches the entire acquired window by each original MessageReference and recipient identity. It indexes only the acquired rows, computes each delivery reference once, and yields the original delivery with original receipt objects. Missing receipts still use the existing UnrecordedNotificationSource; queried duplicate receipts still fail. Duplicate rows outside the requested deliveries do not invent a new rejection. No state persists beyond the acquired window.

Both live and recorded MessageNotification projections use the same changed owner through their existing shared project_delivery_window. The old single-delivery scanning method is deleted; no alias, cache, new registry or decoder is added.

## Verification and remaining work

Original audit.Package parsed 324 source, 378 test and 53 tool modules with zero omissions. One for_delivery caller existed, in the shared projection, and is migrated. Changed modules compile and diff check passes.

Four affected real-bus/SQLite checks pass in 2.41 seconds: missing-receipt/rename projection, original handling updates, shared open-source notification projection, exact receipt identity and duplicate rejection. Initial pytest launch refused unavailable configured xdist options before collection; the same focused check ran with repository addopts cleared, without parallel workers.

This verifies notification semantics at the source/database boundary. It does not establish reduced live frame time. Other observed work includes route validation on the UI thread and repeated record decoding; those remain separate concrete tracing work. No installed package, active route, native process or public session was changed.
