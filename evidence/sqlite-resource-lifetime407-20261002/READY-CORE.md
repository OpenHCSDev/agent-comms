# Core537 installed source checkpoint

Source checkpoint: `0c859bc3ee523bdf960b91f962f791d72d313ac7`; production byte-equal to `cd150be078c07b9f4b2d1af83e4cd28f1f584597`.
Base: `8f0e4d92e93000c4c7b7bf83e4da56544c10eb66` (actual330).
PR: https://github.com/OpenHCSDev/agent-comms/pull/537

## What changed

The existing CoordinationStore owns bounded read-only snapshot acquisition, rollback and close. It pins the original committed snapshot before any schema decoder runs. SQLite's numeric BUSY/LOCKED outcomes become CoordinationReadUnavailable with the original cause; they never establish missing rows, successful empty history, unsupported schema, or a stale CAS revision. Caller budgets are unchanged. Constructor setup also closes on configuration failure; initialization and write admission remain separate.

All migrated consumers: native publication/stage/source reads; original recipient handling; passive goal failure; optional awareness and its live-inclusion recheck; continued private context; compaction read-only observation; recovery projection and gateway. Journal writers, irreversible admission and provider/prompt behavior are unchanged. The original gateway's authenticated, bounded canonical identity and execution prechecks and RecoverySelection.project borrow ONE connection/snapshot.

SchemaMeta.require_current owns both user version and schema metadata decisions. The gateway no longer repeats either comparison or reads metadata independently. RecoverySelection.project reports unsupported_schema on the existing SchemaVersionError boundary. No proof flag, wrapper, extra store, cache, schema, native artifact, timeout or retry policy was introduced.

Production scope: 10 files, **304 added / 341 deleted**. The old free projection procedure and all its references are deleted; the existing RecoverySelection carries its work. Patterns: IMPL-13 resource lifetime, BOUND-2 existing owner, IDEN-7 read custody.

## Installed checks and what they detect

Normal wheel installed by declared requirements into the existing owned536 author environment. `installed-source-proof.json`: all311 Python files equal candidate source; installed import, wheel SHA and direct_url recorded. No source PYTHONPATH override, public prefix edit or native build.

`installed-sql537-03.log` / XML: **4 passed in1.45s**:

- Actual canonical private wire/participants/delivery assignments under native-style BEGIN EXCLUSIVE: handling and native source acquisition raise typed unavailable (including original SQLite code/cause), recovery reports busy. After release the same original receipts and native schema are readable; wire bytes unchanged. Detects missing/empty/schema misclassification of contention.
- Actual gateway snapshot with concurrent canonical owner-generation mutation awaiting COMMIT: the old original view completes, writer commits only after resource release, next read sees the new canonical owner. SQL trace verifies ONE user_version and ONE schema_meta read. Detects the nested-reader deadlock and duplicate metadata read.
- Real read-only write/invalid SQL and callback failures: non-busy engine failures remain visible, failed connections close, original participant data remains unchanged. Unsupported user version remains unsupported_schema. Detects leaked read resources and incorrect exception classification.
- Existing compaction absent/unsafe/read-only control: no file creation or WAL sidecar on unsupported stores. Detects accidental installation/write authority in a reader.

The first launch hit repository-wide optional pytest addopts absent from this small environment; second launch exposed a pre-existing obsolete ready() fixture (assignment_ids keyword is already absent at base8f0e) and a trace assertion that omitted quoted SQL table names. Logs preserved. The new resource control uses the actual typed participant owner; the quoted-table assertion was corrected. The obsolete unrelated execution fixture/test was not rewritten or claimed passing.

AST evidence: existing NRA Package parser before/after; production311, tests357, tools53 parsed with zero omissions. after-ast records whole-root syntactic consumers and zero removed procedure references. Python-only dotted-call inventory does not prove dynamic resolution or parse external SQLite/native JavaScript.

## Limits and next step

This qualifies the Core read-resource correction, not live UI acceptance or exact original SQL attribution. The actual407 crash traceback identifies a retained SQLite OperationalError surfaced through worker retirement; it does not identify the originating database/statement. Original negative frames/logs remain protected.

Arendt owns paired Toad332 publication/history/status/paging consumers: preserve pending original work and retry via existing coordination_observed, without a new timer or state mirror. Its paired installed path must show busy→release→eventual original source/handling publication. Parent alone owns the next actual configured channel acceptance/public activation; no new provider/input/replay/public write from this checkpoint.

Owned output: this evidence directory, `.artifacts/wheels-sqlite537` (one small wheel), and the reused `.artifacts/runtime-owned-observation536`. Actual330 prefix, native bundle, public root, saved sessions, UNKNOWN dispositions and negative proof originals are untouched.
