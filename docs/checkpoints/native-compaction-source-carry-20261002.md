# Native5 to Native6 compaction source carry

Owner: Mendel, outside `src` only. Parent owns release and live cutover;
Einstein #506 owns input provenance and compact source declarations; Arendt
#489 owns native lifecycle, journal readers and schema. Base: `95d7c03a`.

## Required relation

The existing stopped-custody carry must preserve the exact
`SelectedSummaryAttempt.source_json` bytes referenced by a native intent's
`selectedSummarySourceDigest`. Journal reset, rehashing an original intent,
rewriting native proofs or treating an archive as live enrollment/coverage
proof cannot satisfy this relation. UNKNOWN remains UNKNOWN.

Trace the current producer, original source declarations, every journal/source
reader, native intent linkage, enrollment and input coverage before extending
`tools/cutover`. No production compatibility codec, mirror or second store.
The current operator covers coordination and prompt bindings only; it preserves
the compaction journal without converting its source contract.

## Current boundary

Einstein confirms #506 is replacing full stored-input copies with ordered
original `InputProvenance` plus mandatory `TextDigest`. The compact-reference encoding was published at `de7cd35f`; the determining
owner is closing the historical-attempt reader contract. The former scalar ingress/digest and nullable pending-input
source are not equivalent to the new reference contract merely by key names.

Implementation follows the final declaration and authenticated old source,
never a guessed migration. If an unchanged historical source proof cannot also
be decoded by the single current runtime format, document that conflict and
request the determining owner's correction before writing the operator.

Actual copied-original stopped acceptance comes last. No provider calls, input
replay, live owner signals, journal reset or public installation is authorized
by this draft. Preserve original journals, native sessions/proofs and donor
prefixes. Pattern leads: IDEN-1, IDEN-5, BOUND-2, TIME-9.


## Actual running-source inventory

Source observed: `/var/tmp/agent-comms-live-20260927-wzjtqhza/compaction-commits.sqlite3`.
Classification: **running-source-inventory, not stopped carry**. A mode=ro,
query-only SQLite transaction supplied schema, counts and a private logical
backup. The source file revision was unchanged; observed SHA256 was
`296e6da164bd7e16fc086e83a9f509dccdf12c4e33876c1f854b26a78d9132b1`,
446464 bytes. The inventory took 0.038554 seconds; no public write, native
input, provider call, owner stop or replay occurred.

| Canonical table | Observed rows |
| --- | ---: |
| selected_summary_attempts | 4 |
| operations | 3 |
| publications | 3 |
| private_raw_inputs (UNKNOWN) | 341 |
| enrolled_private_sessions | 0 |

All four sources use the original scalar `ingress_key`/`original_digest`
encoding. Three attempts are linked to committed native operations; all three
`selectedSummarySourceDigest` values equal SHA256 of their exact original
`source_json`. One attempt is refused and has no native commit reference.
These facts do not grant new original-input admission or change disposition.

Each attempt's frozen retained facts contains exactly one original input with
the same ingress key, explicit human origin, and text digest equal to the
recorded original digest. This supplies a concrete provenance reconstruction
lead from frozen evidence rather than today's registry. The shape/equality
inventory is not declaration authentication: the outside-src carry must decode
with the authentic original declarations and certify the whole target request.

Sanitized observations are committed under
`evidence/native-compaction-source-carry-20261002/running-source-inventory01/`.
Only hashes, shapes, counts, resource revisions and paths are exposed. The raw
logical SQLite backup remains private (0600, parent directory 0700) under
`/home/ts/.cache/agent-scratch/comms-native-compaction-source-carry-20261002/running-source-inventory01/`.
Its hash is `4d48c65ab94a77eeecc8c5aaf026b7184d632252736a3897025bbc3e6792dee4`;
it is not a byte-identical copy or a stopped donor. No raw database is committed.

## Determining owner and remaining carry relation

The strict current decoder cannot decode historical scalar source bytes, while
rehashing those bytes would invalidate the original native commit reference.
Einstein #506 and Arendt #489 own separating the immutable original native
proof input from the one current typed semantic source on the existing attempt
and closing every source/envelope/outcome/admission/recovery consumer. Mendel
#510 extends the existing stopped carry only after that declaration/DDL is
published. Original source bytes, native payload/metadata/reference digests,
operation and publication states, all UNKNOWN rows, enrollment and coverage
must survive unchanged. No runtime old reader, mirrored request authority,
reset or archive-as-admission is acceptable. Patterns: IDEN-1, IDEN-5, BOUND-2,
TIME-9. No carry acceptance or installed-readiness claim is made here.


## Authentic installed declaration check

The installed Native5 interpreter from
`/home/ts/wt/toad-receiving-native5-batch490-20261001/.artifacts/runtime-certified508-corrected-candidate-20261002/bin/python`
read only the private inventory backup with query-only SQLite. Installed Core
`74877f2dd108ed211871a098064178eaf3a4fdb5` was confirmed by its wheel
`direct_url.json`. Its actual `JournalTable` declarations exactly match the
observed SQL DDL. All four attempts decode through its original
`SelectedSummaryAttempt.envelope()` and round-trip to the EXACT original
`source_json` bytes. Existing `ExactTaskFact.original_sources()` supplies exactly
one recorded `InputProvenance` for each original ingress; its original human
origin and text digest certify the shape/equality lead above. No changed schema,
current provider request, admission ACK or public write was created.

See `authentic-installed-declaration-inventory.json` beside the other sanitized
receipts. This is source authentication on a running-source logical backup,
not owner-stop custody, native execution or cutover acceptance.

Einstein reports Arendt's granted target contract on the existing attempt:
`operation_id`, `session_file`, opaque original `source_json`, mandatory typed
`request: SelectedSummarySource`, then `state`. The derived target SQL adds
`request TEXT NOT NULL`; the reserved/unknown unique state index remains.
All semantic source/envelope consumers must use this ONE typed request; native
commit verification continues hashing only the untouched original bytes.
The published determining-owner production checkpoint and complete Native5 to
Native6 schema relation remain prerequisites for extending the carry operator.
The original root is still running and no stopped donor is claimed.


## Published source-contract implementation checkpoint

Einstein published `62719ea96dc646d6dff0d717ae747d9bc6ceacb3` for mandatory
`SelectedSummaryAttempt.request`, after compact-reference checkpoint `de7cd35f`.
Arendt confirmed the determining release tuple: original `9/3/3/5`, target
`9/3/3/6`. Cohort schema 2 is separate; prompt binding has a DDL digest, no
invented numeric binding version. The target journal has exactly one current
request and opaque historical original bytes. No product code is changed here.

Four existing outside-src files now close the producer/resource/consumer path:

- `native_schema_carry.py`: derives both old/current journal DDL from actual
  `JournalTable` members. Native5 coordination/binding facts remain identical;
  only declared Native6 DDL/metadata and the journal's required request column
  change. Existing `CarriedNativeStore` is now a declared operation family;
  filenames, carry and physical publication derive from its members rather
  than filename dispatch spread across acquisition/prepare/install.
- Original decoding runs in the actual installed Native5 interpreter. Its
  original classes read and round-trip frozen source bytes. Required refs
  derive from certified retained `InputTaskFact` objects, including the only
  nested input-provenance task consumer, `NativeInputConstraintPin.subject`.
  Missing or ambiguous original evidence refuses conversion. No mutable input
  ledger/registry join, generic JSON walker, FieldCodec subclass or old product
  reader is introduced. Goal/GoalMentionSource own different identities and
  need no guessed digest.
- The target uses current `FieldCodec`/`SelectedSummarySource` and the actual
  request column's declared SQL representation. Original selected rowids,
  operation/session IDs, source bytes and state cells remain exact; operation,
  publication, UNKNOWN raw-input and enrollment rows are unchanged. Every
  original native source reference is checked against untouched source bytes.
- `retained_summary_reset.py` projects the same acquired descriptors for files
  a declared carry leaves unchanged. `runtime_installation.py` verifies all
  originals before publication, retains original evidence and then verifies
  remaining resources. It no longer claims the replaced journal's named inode
  is unchanged after a successful declared publication.
- `native_schema_carry_controls.py` removes the old Native4 synthetic session/
  proof seeder. The final control consumes an actual private stopped Native5
  copy. A separate explicit running-inventory mode exercises only conversion
  of the real journal backup and never claims stopped release custody. Both
  use the same existing carry implementation; neither launches a native owner,
  prompts a provider, fabricates a fresh enrollment or grants input admission.

Measured code-only diff across these four tools: **411 lines deleted / 451
added**. Obsolete Native4-to5 column removal, TRIAGE singleton construction,
route construction and fixture proof writing are deleted. New-case check: one
store member owns its file/carry/publication; acquisition/prepare/install derive
membership. There is one current format, with no earlier-release branch.

Sanity performed after the source change: all four operator/caller files parse,
`git diff --check` passes, the actual old interpreter emits the authentic
`9/3/3/5` declarations and successfully projects all four real frozen requests.
`authentic-original-producer-check.json` records sanitized output. Raw request
transfer stays private 0600 in the named scratch directory.

**Not Ready for carry activation.** Arendt and Einstein confirm no coherent
installed Native6 target prefix exists yet. Source-overlay checks do not
substitute for that installed target. Final Native6 source freeze/one normal
receiving package is required, then invoke the existing installed operator
against this real running-inventory backup for an explicitly limited control:

```sh
<reviewed-Native6-prefix>/bin/python tools/cutover/native_schema_carry_controls.py \
  /home/ts/.cache/agent-scratch/comms-native-compaction-source-carry-20261002/journal-control01 \
  --source-python <original-Native5-prefix>/bin/python \
  --running-journal-inventory /home/ts/.cache/agent-scratch/comms-native-compaction-source-carry-20261002/running-source-inventory01/compaction-commits.running-inventory.sqlite3
```

The final whole operator uses `--stopped-original` with a privately retained
actual stopped Native5 root under its control directory; parent provides the
stopped-custody grant. The running-source backup is never substituted for that
release qualification. No duplicate package/native build, provider repeat or
public cutover was performed to manufacture acceptance.
