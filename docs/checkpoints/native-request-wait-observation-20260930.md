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
