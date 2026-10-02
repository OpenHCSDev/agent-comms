# Goal ledger schema ownership

Arendt owns the production GoalLedgerTable/TypedTable contract; Mendel owns the
existing stopped-store installation/carry. Parent alone applies any public carry.

The actual agent-comms-ux goal was saved before tool-end bookkeeping reopened its
ledger and refused it. Both schemas claim version 6, but the original
FailedTurnEvidence.reason CHECK lacks the newly declared model_request_failed
member. No goal must be set again and no STARTED/UNKNOWN input is replayed.

The source pass follows existing SqlStorage.constraints, TypedTable.schema_objects,
GoalLedgerTable member declarations, GoalAttemptSchema initialization/validation,
GoalTurnAccount store reopening and read_failed_turn_projection. The generated
DDL remains the schema authority. The manually maintained marker must derive its
identity from those same declarations; the existing stopped carry consumes them
and preserves all original rows before target readers launch. No worker migration,
new schema catalog, per-reason SQL list or live mutation.

Implementation follows source analysis. Validation is last: the related schema
and carry changes are checked as one batch and one installed affected goal path.
