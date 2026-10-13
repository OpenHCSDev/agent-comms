# PR 768: canonical codec and shared part normalization

Owner: core-route-counts. Fresh instruction 3754 continues distinct source work;
failed input 3518, its WebSocket terminal failure and blocked goal state are
preserved. No provider/input replay occurred. Normal checked edits now succeed.

Deleted the handwritten source/segment/span/root field validators. The existing
FieldCodec validates ManifestSources tables, all original declarations and the
complete reconstructed ContextManifest. Source references remain strict integer
indices with table-bound checks owned by ManifestSources. ManifestPart supplies
one normalization algorithm inherited by SegmentManifest and
ContributionCoordinates; nested parts come from their field capabilities and
cached declaration annotations. Empty contributors use the existing declared
wire_omit_default mechanism. No compatibility reader, additional store or codec
subclass is introduced.

Source evidence: refactor-audit Package parsed 305 production modules with zero
omissions and enumerated 153 relevant class/consumer sites before editing.
SDK producer/inspection sites in stack/native-turn-context.mjs were read;
JavaScript is not covered by that Python AST pass. The root declaration still
owns measurements, positions, request identity, sources and captured text.

Concrete before/after boundary check: the clean-export installed package from
the previous code checkpoint accepted a malformed boolean token count. The new
implementation refuses it through FieldCodec: Expected int, received bool.
The regression also checks unused malformed shared tables, string byte counts,
invalid captured text, unknown keys, bad indices and exact manifest round-trip.

Focused batch: 74 passed, 1 skipped, 3 deselected; one context-socket failure was
caused by inherited PI_AGENT_ID/AGENT_COMMS_THREAD naming an unregistered caller
in an isolated root. The same context-socket path passed after isolating those
environment variables. Deselected checks are the existing installed-only
callback and stale claim/selected_response fixtures; not full-suite acceptance.

Saved-history carry/install executable already returned success on the frozen
7,513,177-byte archive: 8,400 message rows (watermark 12,544), no observations.
The original bus/marker/checkpoint/registry hashes stayed unchanged. Direct CLI
construction treated its historical registry as current and refused obsolete
last_goal_report_turn. That is not a saved-history read pass. Qualification must
use the existing HistoryArchive/RegistryProvenance owners, not strip fields or
add a legacy Thread reader. That direct-root failure is resolved for historical reads by the existing
historical owner, as the following installed verification demonstrates.

## Installed saved-history and actual-observation results

Integrated current main normally, including its declaration-owned
selected_response segment. A clean committed export/wheel was installed into
.artifacts/pr768-install-3754/runtime, not the product runtimes. Its installed
history CLI successfully reads both complete frozen archives through
HistoryArchive.attach and RegistryProvenance: 8,420 messages, with every original
message field and source identity preserved. The first archive reads in 2.33 s.
The second archive also exercises the new installed carry executable's explicit
--install-at: 20 rows, 0.67 s, valid final-location seal and byte equality.
Both originals' bus/marker/checkpoint/registry hashes remain unchanged. No
provider call, live history command, live rewrite or runtime relink occurred.

Separately, three actual previously retained complete observation rows
round-trip and publish/read through the installed package's canonical writer
and context index. Exact original manifests, request IDs, contributors, source
membership and measurements survive without journal lookup. Compact byte sizes
are 217,449 / 385,293 / 107,097 versus 871,644 / 1,602,420 / 351,950: 74.9% smaller
in aggregate. Combined cold full-form decode + encode + decode times are
0.44 / 0.55 / 0.11 s, not comparable to the earlier encode-only timings. The raw
tail has an incomplete leading fragment which is excluded; it is not a
certified complete history and cannot qualify full historical manifest carry.

Current-main checks: 76 passed, 1 skipped, 1 installed-only callback deselected;
the stale checkpoint claim fixture lacked exact_target at both API calls.
Updated it to use its existing case.reply_target; that check now passes (0.54 s).
After AST evidence: 306 parsed production modules, zero omissions; only
Provenance and ManifestPart declare compact/expanded algorithms. Both part
consumers inherit the same implementation; tables and nested parts are derived
from their declarations. JavaScript and dynamic annotation resolution are not
claimed as Python AST proofs.

## Bounded original-prefix carry: request 4006

The previous request for an approved sample was unnecessarily broad. A safe
bounded source can be constructed without another approval: the first indexed
actual manifest ends at byte 977,156. Captured the contiguous original byte
prefix [0, 977156) under the existing CertifiedSourceRead shared lock, without
copying the complete bus or modifying its bytes/metadata. The prefix contains
283 original rows: 282 messages and one full-form manifest, through sequence
282. It is a complete contiguous prefix, not a synthetic fixture or selected
rows reassembled into invented history, and not a complete live-bus clone.

The existing installed full-form reader and install_private_bus_checkpoint
certified that isolated WireLog as immutable ArchivedAccess. Thus the existing
checkpoint owner, not a new exporter/publisher or production legacy reader,
constructs its final certificate. The installed #768 carry executable then
converted and installed it at a separately certified final location:
977,156 -> 947,688 bytes, 283 rows, one real historical observation.

Installed verification passed: all 282 original message fields match; the exact
original manifest, owner and index geometry match; the final seal validates;
HistoryArchive.attach and its installed history CLI return all 282 messages in
0.95 s. The immutable original prefix hash is unchanged. No provider/input
replay, live history command, full-bus copy, live mutation or package change.
3518 and UNKNOWN originals remain untouched. Initial verification setup errors
(list-versus-iterator and absent copied registry guard) were known failures,
not uncertain writes: corrected the former and used PrivateRegistryGuard to
initialize the isolated final registry. Original/candidate evidence preserved.

This closes the bounded retained-manifest carry verification gap. Full live
cutover and coach review remain outstanding; once-per-digest captured-text
retention is still unfinished. Do not autonomously merge or deploy.

Owned package/export: .artifacts/pr768-install-3754; older
.artifacts/pr768-install-3518 is superseded. Owned disposable checks/output:
codec-3754*, context-socket-3754, current-main-3754*, exact-target-3754 and
installed-history-3518/3754 under
/home/ts/.cache/agent-scratch/core-route-counts. Concise JSON results describe
these actual installed checks. Retire generated files after borrower checks;
retain source, concise results and UNKNOWN evidence. The additional owned
bounded-prefix-4006 directory under the same scratch root holds original-prefix,
candidate, final and viewer, with prefix-result.json, carry-result.json and
verification-result.json. The complete bounded export is under 1 MiB of bus
bytes, rather than a multi-GB copy; keep its original until review/acceptance.
