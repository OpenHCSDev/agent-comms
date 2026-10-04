# Ready: original archived history

Source: 17b9ba96b384fc9d04392b9f263904f5ab37a41a. Production unchanged by the
final evidence/control commit. Draft Core559.

## What changed

Existing `WireAccess` declarations now own mutable versus archived checkpoint
opening/verification, page-index lifetime and context-observation reading.
`WritableAccess` retains full current CheckpointTable membership, pending-intent
recovery and append-only suffix adoption. `ArchivedAccess` opens the original
SQLite certificate with mode=ro, uses the unchanged `PrefixCertificate` declaration,
and requires its original final seal, exact DB and bus inode/revisions, marker
root/sequence and prefix tail. Pending seals, replaced/changed bus or DB refuse;
no archive recovery or resealing exists.

The observed originals have response_keys/delivery_sources/addressed/prefix_certificate.
They lack the later ContextManifestSources table/index. Their PrefixCertificate
DDL is exactly the current declaration; their other tables are not decoded as an
old runtime format. Original byte certification is independent of today's
writer-derived index membership. Archive context observations read the original
strict WireScan through that same certificate. Indexed delivery/conversation
pointers still decode exact original bytes and frozen audiences through existing
CertifiedSourceRead; current native-input/handling authority is unchanged.

BusPageIndex remains the one disposable page-offset owner. Current access may
sync it; archived access opens it query-only and checks the same source revision
and tail. Unusable derived offsets follow the existing original-byte reader.
The archive creation owner prepares offsets while its new stage is still private;
published archives are never rebuilt. Sparse archived reads retain warm offsets.

The two existing installed-history helpers now reference original immutable
sources. Deleted their source-copy/registry-guard recreation/certificate deletion,
marker reset and index rebuilding workaround; all callers migrated to
reference_actual_history. Original provenance/aliases stay on HistorySource.
No marker/schema/protocol/native/package pin change or frontend API change.

Five production files: 19 lines deleted, 140 added. Existing helper/control files:
48 deleted, 62 added. No new production class, registry, cache, codec or mirror.
Patterns: IMPL-4 (existing access family carries its behavior), BOUND-2 (existing
PrefixCertificate owns its row), TIME-3 (deleted archive reset/reseal workaround).

## All consumers

Before/after AST uses existing NRA Package over src/agent_comms, tests and tools:
725 parsed modules, zero parse omissions. See before-complete-ast.json and
after-ast.json. Attribute/callback matches are source candidates, not proof of
dynamic resolution. No frontend protocol change; no frontend source edits.

HistoryArchive.page/integrated_page and HistoryView.full_history feed the same
MessagePageRequest. Channel/DM/all-history CLI, bus pages and display preparation
therefore select access behavior once under the original bus lock. Historical
Transcripts uses AssignedTranscriptSource -> WireLog.conversation_sources ->
original certified pointer read. StoreLock's construction of a fresh WireLog
keeps working because access comes from the original metadata, not a caller-side
subclass. ReadLedger paints original provenance and inode; its human display
acknowledgement remains separate from native delivery/handling. Neither the
receipt nor the installed run grants historical rows current input authority.

New readonly is an SQLite connection mode, never retained lifecycle state.
Missing PrefixCertificate.one is external SQL absence, rejected explicitly.
No new None-valued domain state or generation/status copy.

## Verification and actual path

102 initial source controls passed; one warm-page control found full raw rescans.
Corrected within the existing index owner. Its final five controls pass in 2.55s:
warm sparse reads, backward/forward archive traversal, changed snapshot refusal.
Archive bus/DB mutation and pending-seal controls pass 3/3 in 1.42s, including
actual query-only SQLite write refusal and unchanged bytes after rejection.
No unaffected matrix repeated. Initial failed logs retained.

Normal wheel installed in released existing485/488 holder. 339 installed source
files byte-equal, SDK0.12.1, normal dependency resolution, 69-package pip check.
No new environment/worktree/native build, source overlay or dependency bypass.
See installed-source-proof.json, install-final.log and pip-check.log.

Actual installed CLI, no AGENT_COMMS_* or PYTHONPATH overrides:
`agent-comms history --channel '#openhcs'` exit0 in 0.845115s, 214 messages.
Original traceback failed when this traversal entered an archive. There are no
#openhcs messages in those original archives; success traverses their empty scope.
Separate actual original #comms page returned five HistoricalMessage rows with
no current-turn authority. Original historical NRA native transcript returned
25 events in 0.192318s, through the original saved file and receipt composition.
Current-page read returned five rows in 0.022484s.

Both archive bus/marker/certificate/page index/registry/catalog files,
history_sources.json, original NRA native history and .input-proof hashes remain
exact. Originals: 104+7 thread declarations and 9+0 aliases; their sealed wire
cuts 12544 / 20 and 7,513,177 / 27,674 bytes. No input, provider call, replay,
owner stop/restart, archive reset or public installation. This is installed CLI
and native-history read acceptance, not physical UI or latency-wave acceptance.

Attempt01's CLI already succeeded; its authored assertion wrongly required old
#openhcs rows. Raw log/receipt remain unchanged. Corrected scope verifies actual
#comms originals separately; it does not claim a production failure was fixed
between those attempts.

See installed-receipt.json and installed-original-history.log. No owned child
process remains. Scratch wheel848KiB at ~/.cache/agent-scratch/
mendel-archived-wire559-20261003/wheels remains a current installed direct-URL
donor; protect until the receiving wheel replaces it.

## Release

Parent owns normal receiving pin/publication. No archive schema reset or carry is
needed: original sealed sources stay untouched. Public default still old reader
until the receiving package includes559. Original555 preparation/timing work stays
independent; this read fix is not a claim to solve the 99s summary stream.
