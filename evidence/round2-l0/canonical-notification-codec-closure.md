# Canonical notification and wire-codec closure

Parent229 after a732577. Removed schedule_private_candidate_after_commit and
its old-protocol marker/size probe. Messaging schedules the existing maintenance
owner directly after each successful canonical publication, outside publication
locks and inside the existing post-commit exception boundary. Notification failure
cannot report the durable original as failed. No new scheduler/store was added.

MessageWireCodec no longer overrides float encode/decode to accept NaN/infinity.
It inherits finite JSON and preservation of integer/float spelling from FieldCodec;
only the existing specialized claim boundary remains. Removed the obsolete public
human append test and non-finite full-text-export cases. Recent cutoff still covers
undated timestamps and future records. Added finite JSON refusal and exact numeric
spelling/message identity coverage.

Actual retained stage:120current +8400archive +20archive messages all read through
the current codec, serialize with allow_nan=False, retain message identities and
canonical envelope digests on reopen. Source histories were not rewritten by this
check. An initial probe used a wrong helper name; corrected it to the actual
public_envelope_digest owner before running the completed check.

36 focused notification/export/human-ingress cases pass. Includes actual writer
processes, post-commit lock release, cancellation and UNKNOWN cases. Fresh Python
process with no test patches publishes a canonical initial, indexes its sole
candidate, verifies one committed row and exits with maintenance finished. No
provider call. Failed intermediate tests/logs remain evidence: old nonfinite
export expectations and stale target-error assertions were corrected for the
current canonical contract; behavior/durability assertions retained.

Other lexical edits correct stale descriptions of code already deleted (notably
backend text mode), name the missed-watch poll interval directly, and describe
current unbound-claim corruption without implying another runtime reader. Existing
manual collaboration links remain independent editable domain data; the previous
comment incorrectly called them legacy. The tool catalog is not rebuilt: original
POST-FEATURE-DEBT-AUDIT.md explicitly rejects that unsupported mirror inference.
External openai/internal/shims.mjs remains a required native package path.

Remaining L0 reader/bootstrap code in native_source_cursor/acp belongs to Cicero;
the package-wide guard is not yet claimed clean. Native243/244 will also update
backend code. Final D22 migration/reset and deletion of executed tools remain open.
