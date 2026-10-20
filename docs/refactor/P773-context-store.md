P773: census before separating recorded context from message bytes

Source baseline: 6b7c0f6211c9dc07a0655521054e9be4fb90ffa2.
Existing refactor-audit Package parsed 405 production modules, zero omissions.
AST evidence and source searches: owned scratch P773/ast-before.txt,
all-manifest-consumers.txt, saved-offset-sites.txt and offset-declarations.txt.
Lexical names are not proof of dynamic receiver identity; the sites below were
read semantically. No production files have changed at this checkpoint.

Writers and declarations

- turn_context.py:1112 RecordedContextManifest.capture/restore owns request,
  thread, turn, counter, segment identities and per-owner recording sequence.
- turn_context.py:383 RecordedSegmentManifest owns previous (physical bus offset),
  inherited_entries, entry references and segment reconstruction. ContextSegment
  record_manifest/record_text and TranscriptSegment.record_manifest select text
  reuse and transcript keyframes. ContextEntryManifest owns entry content;
  ContextEntryReference owns native session plus entry identity.
- pi_events.py:469 and agent_events.py:76 carry NativeContextManifestData.
- turn_progress.py:109 and selected_participant.py:131 invoke its record method.
- native_turn_context.py:29-40 NativeContextManifestData.record builds the original
  manifest and invokes WireLog.record_context through Coordination.run_worker.
- wire_log.py:672-681 reads the prior manifest and appends context records under
  the checked bus lock. CheckedRead.context_records at private_bus_checkpoint.py:
  593-612 selects missing entries and emits both observation kinds.
- wire_record.py:80-213 declares ContextEntryManifestWireObservation and
  RecordedContextManifestWireObservation, authored/deferred observation envelopes,
  checkpoint extraction, and silent/no-sequence behavior. WireRecord.from_wire
  and public_from_wire currently accept observations alongside messages.

Readers and original-source validation

- private_bus_checkpoint.py:63 CapturedWireSource binds PrefixSource, pointer,
  original bytes and decoded value; CheckedRead.capture_resources/capture_sources
  preserve and validate that binding across reads.
- ContextEntrySources (:839) decodes entry observation rows by offset/length.
- RecordedContextManifestSources (:942) latest/resolve/for_owner/decode_bytes
  reads manifests, predecessor rows and entry rows; validates root, physical
  prefix, same-thread predecessors and entry completeness; pages by owner.
- ContextManifestCursor/BeforeContextManifest/AfterContextManifest (:875-905),
  ContextManifestReference (:906) and ContextManifestPage (:921) retain original
  source identities and physical manifest offsets, including page reuse.
- wire_log.py:680 exposes context_manifest_page on the message log.
- cli_commands.py:1579 ContextCliCommand.manifests and selected-turn/diff commands
  consume these pages; RecordedContextManifests is the CLI result declaration.
- runtime_requests.py:457 RecordedContextRuntimeRequest.record contains a
  ContextManifestReference. read_manifest (:463) supplies comms.bus.log; context
  reference, recorded segment and annotation requests inherit this read.
- Toad core/context_inspection.py:519 reads context_manifest_page;
  its RecordedContext/Page states retain page resources and exact references.
  widgets/context_explorer.py:39 holds the typed cursor. These must move with
  the Core page API, not keep reading WireLog.

Every located bus-byte position / source cut

- private_bus_checkpoint.sqlite3: DeliverySources.offset/length (message bytes),
  ContextEntrySources.offset/length and RecordedContextManifestSources.offset/
  length (observation bytes), prefix_certificate CheckedPrefix.offset plus
  device/inode/digest/tail/sequence. PrefixWitness/PrefixSeal and WireMetadata.seal
  bind the entire checked prefix. Rewrite source positions and reseal together.
  Preserve ClaimCommits, ResourceClaims, ResponseKeys, PublicMessages, Addressed
  and Replies; these include durable decisions and UNKNOWN-gap evidence, not
  disposable offsets that can simply be discarded.
- bus_page_index.sqlite3: BusPageRow.offset and BusPageSource.revision/tail are
  declared disposable source-bound page indexes; rebuild for the rewritten file.
- wake_candidates.sqlite3: CandidateCheckpoint.byte_offset/device/inode/tail and
  sequence, plus derived candidate rows; rebuild through its existing owner.
- bus_route_counts.v<schema>.sqlite3: route_source RecordedRouteSourceRow.revision
  (size is its through cut) and tail_digest; rebuild through BusRouteCounts.
- bus_activity_<schema>.json and bus_display_<schema>_<viewer>.json:
  ActivityCheckpoint/DisplayCheckpoint inherit AppendCheckpoint source revision,
  offset and tail. Rebuild their disposable projections for the new source.
- Opened WireSnapshot/ComputedWireSnapshot, page-index lookups, retained
  CapturedWireSource values, PrefixSource witnesses, and context page cursors /
  ContextManifestReference values hold physical cuts in memory. A source
  replacement invalidates those values; the new reader must bind the context
  store, never quietly interpret an old bus offset as a new-store offset.
- runtime_requests.py explicitly serializes ContextManifestReference. Its source
  root/device/inode and offset are one identity, not just an integer to renumber.
- coordination_tables/annotations.py:23,69 generated offset/length columns are
  UTF-8 segment-text coordinates, NOT bus byte positions. Their declared ModelLabel
  and DisclosureRequest data refer to segment digests and span coordinates. Do
  not rewrite these text offsets as if they were bus offsets. No direct typed
  ContextManifestReference field was found in CoordinatorTable declarations.
  Arbitrary historical payload JSON cannot be assigned receiver identity by AST;
  original diagnostic/request records must not be replayed or relabelled.
- ProvenSourceCoverage and checkpoint_seals consume bus source revisions and
  checked prefix boundaries; conversion must replace the physical cut and
  regenerate its proofs, not merely update two SQL offset columns.
- activity_checkpoint.py also inherits the generic AppendCheckpoint, but its
  ActivityCheckpointStore tracks the separate activity log, not bus.jsonl.
  Preserve it. Sharing the checkpoint base is not evidence of the same source.

Required ownership and conversion

RecordedContextManifest must own its append-only context store. Its entry and
manifest positions must be indexed there, not in the message checkpoint. Segment
versions must reuse the previous unchanged version without serializing its
manifest again. References must resolve to an actual changed version so an
unchanged system layer cannot create an unbounded predecessor chain.

One Carry must split existing complete rows, preserve every message and its
sequence/audience/claims, relocate context and predecessor positions, preserve
claim/UNKNOWN facts, rebuild disposable byte indexes, and reseal the new message
prefix. Runtime and Toad readers then use only the new context-store API. The old
location decoder belongs only to that one-time Carry, never a runtime fallback.

Read-only inspection of the selected live root at 05:21 measured bus.jsonl as
15,661,745,876 bytes. coordination.sqlite3 is 344,129,536 bytes and its actual
schema confirms that its offset/length columns are segment-text coordinates.
The active private_bus_checkpoint.sqlite3 is 280,997,888 bytes and contains the
three physical pointer tables plus prefix_certificate. The separately named
private_bus_checkpoint.pre-context-entry.sqlite3 is a historical backup, not
an active runtime source; preserve it, rather than silently treating it as the
current schema or deleting it. Native covered/injected sequence cursors are
logical message sequences, not byte positions, and must keep their values.

An existing TypedStore can own append-only context/entry/segment-version rows
and stable logical row pointers. That avoids inventing another WireLog with
its message delivery seals. Unchanged segment versions can share one stored
row; context appends and their entry/version references commit together. This
is a proposed implementation direction, not implemented or verified behavior.

This checkpoint is the census, not a conversion or an installation. No live bus,
session or UNKNOWN input was changed. The order's 15.7 GB / 95% numbers have not
been independently remeasured except for the exact bus size above. Conversion bytes and affected live-path
acceptance remain unfinished.
