# Subscription and snapshot ownership

608d5033 remains the bootstrap checkpoint. This continuation changes only Core
runtime.py and transcript_updates.py. AST via NRA parsed Core, Parent Toad and
native production roots: 862 modules, zero omissions. Determining sites are in
before-consumers.txt; dynamic external resolution is not exhaustive.

## Original source path

SubscribeRuntimeRequest.apply sends identity first, then awaits TranscriptReplay
-> worker TranscriptSnapshotUpdate.capture -> TranscriptRead.read. Capture and
the before/after content fences recapture native/file/route/receipt/outcome/reply
evidence; thread_transcript_page reads bounded reverse native records. Scoped SQL
reply relations read the native session header, not all 147MB. Replay sends the
snapshot, then turn state, unknown-input PRESENTATION, options and ready metadata.
The ready/load response carries coordination/goal/queue/cursor facts, **not a
second history page**, despite similar 42/43KB packet sizes.

RuntimeProxy notification and ready consumers both use present_session:
FieldCodec decode -> declaration-owned for_session -> encode. Both now run that
same function through existing Coordination.run_worker, in original order.
Actual attachment aliases, source identity and scope provenance remain distinct.
After the new await, existing closed/client identity prevents retired delivery.
TranscriptReplay also encodes its captured snapshot through the existing worker.
No adapter, new copy store, cache, codec or preparation owner was introduced.

Frontend AgentProcess reads/logs one line then awaits ordered notification
dispatch. SessionNotificationOwner.receive awaits ApplicationValidationOwner ->
PreparedRenderer.submit(ValidateSessionUpdateTask): SDK and Comms decoding stay
in renderer workers. Declared handlers start controller snapshot publication.
CoordinationTranscriptReader.publication submits PublishedNativeTranscriptReadWork
under the original NativeTranscriptReadWork content key. It uses the supplied
page with before/after-await source fences; stale content uses the existing read
owner. Controller checks session/surface identity before publishing CommsUpdated.

Conversation -> TranscriptPresentation.snapshot -> SnapshotPublication joins
original applied UI/native custody, admits the mounted frontier, submits
TranscriptRenderTask, prepares history with actual visible categories, then
mounts under the window lock. Warm restore uses the same read/preparation owner.
Parent viewport, native compositor and four-fragment admission remain untouched.

## The 1.499s gap was not capture time

The retained bootstrap run spent 2.528339->4.026368s (1.498029s) inside the first
identity notification handler. Snapshot logged at4.026596s. The ordered ACP loop
cannot read/log that next packet until identity handling completes. Earlier
broad attribution to owner capture was wrong.

New installed original App/proxy clocks separate transport from dispatch:

- Proxy identity received: 962276.618387.
- Raw snapshot received: 962276.940091; gap321.703ms.
- Snapshot forwarded: 962276.989604; gap371.217ms.
- Snapshot rebinding: 48.524ms, confirmed on a worker thread, not the ACP loop.
- Frontend identity handler: 962276.645611->962277.987452, 1.342s.
- Frontend snapshot logged: 962277.987739, about998ms after forwarding.

The dominant observed interval is first frontend notification handling through
application worker validation. Worker admission/start/import/execute/materialize
and synchronous fact dispatch are not separately timed. Owner capture versus
source fencing/encoding/socket scheduling inside322ms is also not split.

## Qualification and remaining limits

Installed candidate uses unchanged Toad e630280f and native d8ebcd9/126 wheels.
Both changed installed modules equal wheel and source. Original existing owner
477574 remains alive; canonical frontier147260275, four bodies, loading0, no App
error and zero provider inputs. Initialize1.362s, load1.720s, supplied snapshot
admission0.424s, page publication0.055s, open-to-visible4.642s. Previous candidate
was4.747s; individual observations do **not** establish a total opening gain.

Core proxy rebinding is installed-qualified off-loop. The original worker was
not restarted, so TranscriptReplay encoding placement awaits its next reviewed
installation; this attachment does not live-qualify that owner-side change.
Existing queue-alias check passed. test_runtime.py cannot collect because it
imports removed TurnSettledUpdate from the retained baseline; unchanged stale
test/import remains. No full-suite claim.

The existing saved-App observer adds timing only around installed original
proxy/codec owners and calls original ACP main. No alternate snapshot producer,
public/provider replay or owner restart. Private copied settings/state were
removed after App teardown. Candidate wheel/runtime/control remain under
.artifacts/cold-snapshot-workers-20261008. Parent owns frontend integration;
existing renderer validation admission/startup is the next boundary to split.
