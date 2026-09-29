# Indexed native proof cutover — completed 2026-09-29

The matched indexed-proof runtime is live. All 87 existing proof journals were
converted after disposable-copy preflight. Nine idle owners restarted through
their canonical lifecycle; all nine fresh ACP loads and the actual default-App
saved-history journey passed. Native JSONL, UNKNOWN dispositions and coordination
stores were preserved. Independent old-proof backups remain private.

The one-shot converter and its retired-format fixtures are deleted. Git history
in PR378 retains the completed tool; ordinary runtime reads accept only indexed
proof. Do not repeat this conversion on current journals. The actual cutover and
its verification limits are recorded in
[the installed/live receipt](../evidence/indexed-proof-context-deployment/README.md).

The procedure below is retained as the history of that cutover.

## Boundary

The new package and core are a matched pair. Prepare a NEW immutable package
using `stack/bin/prepare-pi-native`; never modify global d396 or an existing build.
The `.input-proof` pathname remains the sole proof authority. Native session
JSONL is never rewritten by this migration. Runtime has only the current SQLite
format; conversion is a temporary operator tool outside `src/`.

## Quiet conversion

1. Finish active turns and stop the affected native owners through their existing
   lifecycle. Preserve original inputs, session histories, input dispositions and
   coordination state. Do not reset them or replay anything.
2. Inventory existing `.input-proof` files belonging to the installation being
   upgraded. Match each with its native session JSONL. Record both names and the
   verified stopped runtime; do not act on other installations' files.
3. With the candidate core available, the now-retired tool was run as
   `tools/cutover/native_proof_journal.py
   SESSION_JSONL NEW_BACKUP_PATH` for each old proof. Backups must be on persistent
   storage with sufficient space. The tool takes the existing exclusive native
   writer fence; it never steals a stale fence. It verifies every prior row and
   native source, creates the declaration's constrained/indexed SQLite store,
   fsyncs an independent backup and replacement, and atomically replaces proof.
4. Check the reported proof-row count and tracked IDs, including the count without
   a context proof. These remain UNKNOWN; conversion is never a live receipt.
   Native session bytes and old proof backup must be unchanged.
5. Activate the paired noneditable core/native package. First prove fresh ACP
   attachment and historical display without sending any prompt. Then exercise
   a new explicit diagnostic input only in the agreed isolated/live test scope.
   Parent owns the installed normal App/painted UI acceptance and activation.
6. After independent review and live acceptance, archive rollback receipts,
   remove owned disposable copies after checking process references, and delete
   the one-shot converter. Do not leave a second runtime format or reader.

## Crash and rollback

SQLite DELETE journal with EXTRA sync provides context-transaction rollback.
Both native startup and Python's query-only evidence connection can recover a
hot transaction; neither emits recovered acceptance. The Python connection opens
existing storage read/write solely for SQLite rollback, then prohibits SQL writes.
An initial schema is built privately and atomically linked into the canonical
name without overwriting it. The only recoverable temporary alias is the matching
inode-derived publication name; arbitrary aliases and schema drift are refused.
Unpublished schema scratch is never context proof. Remove it only after checking
that its owned producer is gone.

A killed converter leaves the existing writer fence. Preserve it and the receipts;
an operator must establish that that exact converter/native writer is stopped
before disposing the fence. Before atomic replacement, the original proof is still
complete; after it, the replacement is complete. A failed final directory sync
remains uncertain and grants no prompt admission. Never truncate or infer success.

Rollback to old core/native plus old JSONL backup is permissible only before any
new native input/history/proof write under the new installation. Preserve all
state before checking that condition. Once new work exists, keep the current
format and repair forward; restoring the earlier backup would lose durable proof.
No automatic rollback, input replay, migration on ordinary reads, or cap increase.

## Acceptance

`tests/test_native_proof_recovery.py` uses the actual pinned CLI, normal saved
sessions and local HTTP provider. It covers proof growth past128MiB, cold reopen,
SIGKILL at commit/schema publication boundaries, old accepted IDs and UNKNOWN
claims, exact-ID dedup after saved recovery and a new explicit input. The former
conversion-only tests and child helper were retired with the converter after
live acceptance. The current declaration/schema drift guard remains in
`tests/test_native_proof_streaming_guards.py`; no current native recovery,
uncertain-input or crash invariant was removed. These complement installed
saved ACP/new-input and selected four-tool publication paths. Historical growth
and converter results remain in `evidence/native-proof-checkpoint/HANDOFF.md`;
the current live receipt linked above records completed activation.
