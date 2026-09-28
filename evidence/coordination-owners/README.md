# Coordinator declaration ownership closure

Scope: original S3 coordinator rows, S7 residual `coordination.py`, C0 caller/deletion
closure. Parent owns integration and live installation. No live root modified.

## Ownership and deletion

- Deleted the 2,910-line `coordination.py` aggregate. No forwarding module,
  compatibility reexports, old-format reader, second registry or schema roster.
- Row families own columns, foreign keys, generated fields, constraints, triggers
  and typed decoding in `coordination_tables`: assignments, executions, attempts,
  responses, publications, participants, recovery and metadata.
- `coordination_schema` derives lifecycle expressions and assembles DDL from
  `TypedTable.members_with(CoordinatorTable)`. The installed table package imports
  its discovered modules; adding a row requires no parallel module/table list.
- `coordination_database.CoordinationStore` owns the SQLite connection, private file
  publication, version initialization, transaction boundary and close. The SQL
  publication validator moved onto `PublicationIntents`, its actual envelope owner.
- `coordination_snapshot` owns cross-record recovery validation and retry policy;
  `owner_fence` owns attempt authority; `private_runtime_schema` owns the existing
  explicit private installation capability. Boundary bounds/validators remain
  shared in `coordination_contracts`.
- Eight redundant transition/pointer helpers had no production caller; deleted
  their implementations and tests of those implementations. Actual lifecycle,
  SQLite edge/CAS/fence/receipt and transactional tests remain.
- Response triggers now group the enforced retry, lifecycle and publication facts
  under their row owner. Every extracted module is below 1,000 lines and every
  extracted method below 100; guarded permanently for the row/schema/database family.
- Production, tests, embedded subprocess code and an executable evidence script
  import the actual owners. Toad origin/main has no imports of the removed API.

## Dependency reasoning before edits

The apparent row SCC is mostly mutually referring FK declarations. `references()`
was already evaluated lazily: each row imports its peer declarations at that
boundary. No string lookup registry replaces it. Row modules depend on schema
capability/lifecycle/typed-table, schema assembly loads the package only when
constructing DDL, the connection owner consumes declarations, and recovery consumes
rows. Database/schema never depend on MutationStore or on live runtime owners.
PrivateRuntimeSchema stays below both base and private schema declarations.

Persisted state: same current coordinator version, tables, columns and wire names.
No reset, converter or data rewrite. Existing data reopening is executed below.
The current MutationStore transaction/recovery implementation remains its existing
owner; this closes the assigned aggregate, not every residual S7 module in PR281's
whole-project audit.

## Evidence

- Original source focused pass: 129 tests,18.79s before dead-helper deletion.
- `focused.log`: first installed run 164 passed,6 failed. Four were one missing
  test import during deletion; one guard compared source path with installed
  path; one expected pre-checkpoint corruption wording. All diagnosed explicitly.
- `baseline-cohort.log`: original d6e9e71e reproduces the corruption wording failure.
  Both changed-size and incomplete-tail cases must fail at the current checkpoint
  barrier, before SQL; assertions now name that exact refusal.
- `focused-fixed.log`: 169 passed,1 failed, exposing the second outdated tail
  expectation in the same corruption test. `cohort-current.log` closes that case.
- `native-installed.log`: 3 passed,24.97s. Real pinned Pi with loopback-only fixture,
  normal peer publication, detached owner wake, durable reply, owner restart,
  second distinct input/reply. Both fresh protocol and base-only database scenarios
  pass. The third case proves drift remains refused without reader repair.
- `persisted-reopen.log`: d6e9e71e creates a real queued execution; installed moved
  owners reopen it, commit pending revision2 and roundtrip its typed snapshot.
- `import-path.log`: imports come from this worktree's installed wheel.
- `test_coordination_ownership.py`: no removed aggregate imports; a new TypedTable
  declaration installs/persists/reopens without schema roster edits; S7 size guard.

## NRA coverage and limits

Before and after scans used the entire src/agent_comms as dependency context,
`--json --raw-findings --json-payload full`, one parser/analysis worker,150s internal
budget and165s wall bound. Completed in30.90s and12.15s; no pattern exclusions.
This CLI payload has no scan_status or analyzed/omitted detector inventory, so no
claim of certified full detector coverage. Saved raw evidence in nra-before/after.

Both scans report three R1 semantic-mirror candidates, not mapping_read or
unmodeled_record_shape findings: two FK reference tuples and context's
_terminal_replay relation. Explicit FK endpoints are relationships, not inventories
of every DeclaredFamily member; replacing them with all members would be wrong.
The tool also conflates same-named AttemptRecord owners in its witness set.
No redundant_type_check finding was emitted in this receipt. Synthesized recipes
were rejected by the tool's safety check. The extraction and complete caller
migration are explicit source edits, not a claimed NRA-proven rewrite. Runtime
behavior is established by the executed tests above, not by source parsing.

No CI wait, provider account request, live state mutation or historical input replay.
