# Native cold-start admission and headless diagnostics

## Observed failure

The human `#comms` message at sequence 6943 produced fourteen failure notices
(6944–6957). All fourteen recipient inputs remained UNKNOWN, without native IDs
or appended native input rows. Their saved sessions were approximately 72–84 MiB.
The exact historical errors were not retained: headless runtime updates went only
to currently subscribed clients. This prevents a definitive retrospective verdict
about each recipient.

## No-provider reproduction

Copied the 88,255,627-byte coherent-owner session and its input-proof journal into
private `/var/tmp` directories. Loaded the installed native build, copied installed
extensions and managed project bootstrap, with an isolated wire and network calls
blocked. Sent only `get_state`, never `prompt`.

- One startup: 1.883 seconds.
- Four concurrent: 1.851–2.142 seconds.
- Eight concurrent: 9.742–10.168 seconds, 390–448 MiB peak RSS per child.
- A later warmed baseline eight-start backend probe completed in 3.66–4.06 seconds.

Thus the fixed five-second readiness deadline is vulnerable to cold-start load;
latency varies with caches and competing work. The initial eight-start result
exceeded it without any provider work. This is a demonstrated failure mechanism,
not recovered evidence identifying the discarded error of every live recipient.

With four advisory-lock startup slots, the actual backend path admitted eight
copied sessions in two groups. They reached the unchanged send boundary at
2.35–2.39 and 4.73–4.81 seconds from the common launch. That boundary deliberately
refused every prompt. All eight completed native capability attestation without a
readiness timeout, and no provider request was possible.

Local logs: `/var/tmp/ac-cold-readiness-eight.log`,
`/var/tmp/ac-cold-readiness-gated.log`,
`/var/tmp/ac-cold-readiness-backend-baseline.log`.
These contain structural timing results, not transcript bodies.

## Ownership and invariants

`NativeStartupPolicy` owns four cold-start slots and the five-second readiness
budget. Owners sharing a wire acquire OS advisory locks before spawning a cold
Pi child. Waiting for a slot does not spend the readiness budget. The slot ends
at capability attestation, before the existing owner/goal send boundary; it does
not cover a model turn. Persistent reusable children do not take a slot.
Cancellation releases descriptors, and OS process exit releases locks. Queued
stop/cancellation cannot send a prompt. No input is replayed and no input,
owner, or goal checks were weakened. Slot files are disposable scheduling aids,
not durable authorization records; their fallback temporary location does not
change the private claim/session guard.

Failed turns persist a private fsynced JSON diagnostic before publishing the
non-waking failure notice with a local file link. The record includes only typed
failure reason, timing/exit measurements, turn identity and source sequences.
Raw provider text, stderr, prompt text, image data and unknown diagnostic fields
are excluded. The record is observational and cannot grant Retry authority.
