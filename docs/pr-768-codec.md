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
add a legacy Thread reader. Installed verification remains unfinished here.

Owned persistent package/export: .artifacts/pr768-install-3518. Owned disposable
checks/output: codec-3754*, context-socket-3754 and installed-history-3518 under
/home/ts/.cache/agent-scratch/core-route-counts. Retire generated files after
acceptance/borrower checks; retain source, concise results and UNKNOWN evidence.
