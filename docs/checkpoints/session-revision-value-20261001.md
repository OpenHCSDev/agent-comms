# Session revision value and complete consumer closure

Requested structural item 4. Release integration owner: parent. Sole source
implementation owner: Mendel, received as one complete surface in draft #468.
Base: main9370a6010c80fba75feaad9e2076f1844d2b44ea. The required
456/458/459/462 stack is merged. Its production tree equals tested970bc527.

## Owned relation

Replace the positional SessionRevision alias in selected_source.py with a
value declaring the native file revision and a named state for input-proof
revision presence. Reuse the existing FileIdentity/FileRevision declarations.
Move backend._session_revision into this owner as the sole observation entry.
No private import, tuple slice/index, reconstructed file identity or None test
may continue to interpret that revision in another module. An unavailable
observation must have an owned meaning; it never supplies source authority.

Start with compaction_outcomes.py, then migrate the entire direct surface:
backend.py, native_custody.py, continued_private_session.py,
selected_summary_admission.py, selected_source.py, compaction_states.py,
owner_compaction_commit.py, reservation_rules.py, compaction_summaries.py,
turn_input_binding.py, owner_compaction_adaptive.py and
owner_compaction_manual.py. Inspect indirect transcript, persistence and native
relations from the existing inventory as part of the same migration.

IDEN-3/BOUND-1/TIME-9: identity and observation belong to their value owner,
not thirteen positional interpretations or a codec adapter. The native
five-field colon revision ABI is external and must remain exact. Compaction
journals are runtime state reset at cutover; encode the nested current record
with the original FieldCodec. No legacy tuple reader, compatibility alias,
SelectedSourceCodec, codec subclass, new store or mirrored source state.

## Acceptance and delivery

Preserve original inode, captured offset, proof-sidecar, truncation and
source-change refusal semantics across manual/adaptive summary, reservation,
interrupted recovery, continued-session and outcome placement. Inspect every
consumer and every revision-producing fixture before changing it. No stat
observation creates enrollment, a native input proof or retry permission.

Use bounded focused checks and the existing actual native/ACP compaction,
source/outcome and failure controls for changed behavior. Source-only tests
do not prove installed readiness. Preserve all failed/UNKNOWN inputs and
original saved sources. This follow-up does not change the frozen critical
release candidate or hold its activation.

Publish the complete working change promptly in this draft, report production
lines deleted, and retain exact source/installed evidence with its limits.
No implementation is claimed by this scope commit.

## Working source checkpoint

All 13 direct callers now use the observation/value owner in `selected_source`.
`SessionRevision` embeds original `FileRevision` and declared input-proof
presence. Missing and unreadable proof observations are distinct; unavailable
observations deny matching/acquisition. No optional tuple remains. Outcome
placement delegates native inode/cut bounds to the value; current reservation
uses whole equality; post-compaction compares original proof observation.
The five-field native colon ABI remains exact and only that external stamp is
flattened. Nested runtime source encoding uses unchanged FieldCodec.

Seven indirect source/history/journal consumers retain their public operations.
Their source digest includes the new nested record, and outcome snapshot digest
now uses FieldCodec for that value. A quiet cutover resets only the declared
runtime compaction journal, not native sessions/input-proof/UNKNOWN/wire/goals.

Production replacement: **74 deleted / 153 added** across the 13 modules.
Source checks: 36 passed/3.31s, one inherited transcript root-body assertion
failed identically at untouched ca9/base970 in 1.00s. Schrodinger explicitly
received its typed body-fixture closure; no assertion was weakened. Installed
native/ACP acceptance is pending; this source checkpoint is not Ready.
See `evidence/session-revision-value/WORKING.md`.
