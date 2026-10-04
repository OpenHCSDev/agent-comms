# F2: One owner for session coverage

**Index:** [README.md](README.md). **Repository:** agent-comms. **Pattern:** IDEN-1.

## What is wrong

"Is this point in a session's history covered?" is decided separately by each record type, each comparing its own selection of fields:

- `compaction_records.py:170` `covered_prefix`: a nine-term condition comparing the session file (as a string against a path), the first entry's id against a witness's session id, the source identity against two revision identities, sizes, the entry id, and the parent id against the witness's leaf id.
- `compaction_records.py:395` `covered_prefix` and `:409` `recorded_prefix`: four other fields.
- `native_entries.py:57`, `:352` and `:558`: three more `covered_prefix` definitions.

Every compaction-coverage fix lands in one of these and leaves the others.

## Target

A session-position value type (session file as a path, session id, revision identity, leaf and entry ids) that owns `covers(evidence)`. Each record type exposes the position it holds; the decision is written once. The nine-term condition becomes one call, and the other five definitions either delegate to it or disappear.

## Done when

`covers` exists in exactly one place, no `covered_prefix` compares identity fields itself, and the compaction journeys pass on the live path.
