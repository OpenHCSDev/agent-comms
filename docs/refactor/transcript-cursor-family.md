# P67: cursor states and shared source names

Depends on the separate P68 Core family checkpoint in #1026. This change does
not hold that checkpoint: it has its own branch and draft PR.

## Owner and consumers

`TranscriptCursor(session_file, offset)` is the abstract position owner.
`NativeCursor` stores only those two fields. `WireCursor` adds one nonnullable
`AssignedSourceCursor`. Source containment, sequence advancement and source
checks belong to the corresponding member; consumers do not switch on classes
or inspect a nullable position. The two constructor boundaries select a member
once: captured typed source creation, and decoding an external saved record.

The family lives beside `AssignedSourceCursor` in `transcript_sources.py`, the
renamed existing source/traversal owner. This lets source and history consumers
use one family without adding import cycles or compatibility exports. The two
function-local `dataclasses.replace` imports are deleted; shared methods use
the existing module-level import. Related source imports are module-level.

FieldCodec's original record encoder is moved into `encode_record`, used by
ordinary records and these custom values. `decode_record` reuses its existing
strict record decoder after the cursor selects its member. No second codec.

Saved data is unchanged:
- `TranscriptCursor.wire_position_key` declares the old `receipts` key once;
  the wire member uses field metadata, and the native member projects null.
- `TranscriptReadIdentity.assigned_frontier` retains its old `receipt_frontier`
  key through the existing field metadata, including content projections.
- No new discriminator or on-disk cutover is needed.

Core consumers: ACP updates, history views, native/external readers, source
presentation, checkpoints and read identities. Paired Toad consumers: transcript
preparation, publication, history, tool output and committed presentation.
Publication now asks the cursor to retain its assigned source while changing
its native prefix, rather than rebuilding cursor state outside its owner.
`CommitClaim.native_current` replaces its previous shared method throughout.
Route and maintenance scope names are migrated across Core, Toad and tests;
there are no old method aliases.

## Evidence and remaining work

NRA before inventory: 1,387 modules, zero parse omissions. After inventory:
1,389 modules, zero omissions (production, tests, tools, paired Toad and the
actual metaclass dependency). Syntax evidence does not resolve arbitrary dynamic
attribute construction. The complete declared sites were read and migrated.

Search results across both production/test roots:
- Old source module and old scope/containment/claim method calls: **0**.
- `git grep -n "receipts is None\\|receipts is not None" -- src/agent_comms/transcripts.py | wc -l`: **0**.
- The two saved key declarations above each have exactly one production owner.
  Moving the cursor does not hide a nullable field: neither member stores one.

Final bounded source check: **46 passed** (cursor saved-data roundtrips,
read-identity legacy-key roundtrip, ACP metadata, record codec, paging and route
scope). A separate gate/metadata batch passed **39** checks. The earlier wider
batch had **63 passed / 8 failed**; all eight also fail on untouched base
`4dea7a351`, in an isolated source control. They are not claimed as passes.

Source CLI help and paired selected-module imports work. Full ToadApp startup
is blocked before UI opening: Toad main imports `ConversationRevision`, which
Core main lacks. That is the P38 pair in Core #996, not a cursor alias to add.
The existing P38 owner is being coordinated through core-read-locks. A bounded
fresh saved-tool-details app check captured that exact import error; the earlier
UI batch failed and exceeded its outer limit. No UI or installed acceptance is
claimed. Retained test roots and logs are preserved. Continue the real installed
saved-history journey after the P38 pair is integrated.

P62 native arguments remain a separate unfinished #1007 checkpoint. No configured
provider fork, TRIAGE-to-FULL result or default installation is claimed here.
