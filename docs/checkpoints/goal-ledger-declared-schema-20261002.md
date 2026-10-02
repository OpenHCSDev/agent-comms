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

## Source implementation

Existing GoalLedgerTable now derives declared_schema and its canonical digest
from its existing TypedTable members. Existing GoalAttemptSchema.current derives
its stored ddl_digest from that schema. Deleted the independently maintained
Literal[6] and both constructor copies. Fresh initialization and both strict
reader paths consume the same original declarations; unsupported stores still
refuse before issuing a grant. No worker performs migration.

FailureReason is declared once in diagnostics.py. Its only durable reason field
is FailedTurnEvidence.reason; SqlStorage.constraints derives that SQL membership.
Generation and AttemptRecord family CHECKs also contribute to this SAME schema
identity, as do indexes, references, triggers and every other registered goal
table. No reason roster or per-reason migration version is maintained.

Consumers: GoalAttemptStore construction/initialize/_connect; GoalScheduler
open_goal_store; TurnGoalAccount reserve/tool_ended; ReplacementGoalAction
retirement; read_failed_turn_projection. All original store-opening consumers
retain strict assert_goal_attempt_schema. They do not repair or retry a store.

The carry must install the declared schema and GoalAttemptSchema.current marker
under existing stopped-store custody, preserve every nonmetadata row and original
preimages, then publish/launch the matching readers. It must not issue ready
grants, resume old failed goals or replay STARTED/UNKNOWN originals. Mendel owns
that continuation. Validation is pending its complete affected workflow.

Patterns: MEMB-1 (manual format identity), MEMB-5 (schema derived from row owners),
TIME-9 (strict current format remains sealed; one-shot stopped carry outside src).

Source census reused refactor-audit ParsedModule over src/tests/tools: 719 Python
files parsed, zero omissions; unique FailureReason/GoalLedgerTable/GoalAttemptSchema
and GoalAttemptStore declarations, six existing goal table members, exactly one
TypedTable field using FailureReason. Fifty-five direct lexical calls are recorded
in evidence/goal-ledger-declared-schema-20261002/source-consumers.json. This is
lexical evidence; imported/inherited/dynamic consumers were read in the source
closure above, not declared absent by the AST.
