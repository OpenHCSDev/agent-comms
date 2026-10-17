## P105 — delete the unused whole-bus ID scan

Stacked on P102 #1113 head **07cf150dd**, as requested. Merge #1113 then this deletion; no independent reader repair is copied here.

Delete WireLog.message_by_id, with no replacement scan, index, helper or alias. Current source had **20 test calls**, not the eight in the task snapshot; migrate the complete family. Production/Toad callers: none. Existing WireLog.messages_for_references remains the reader owner. Message/PublicationReceipts supply their original references. Tool JSON is decoded once by existing PublishedMessageToolDetails before using SentTranscript.source. Batch publication checks use one bounded reference read.

Comparison **07cf150dd → 1a919cb56**:
- `git diff --shortstat -- src`: 1 file changed, 9 deletions(-).
- `git diff --shortstat -- tests`: 7 files changed, 32 insertions(+), 33 deletions(-).
- tools unchanged. No assertion removed or weakened; missing original references now explicitly fail loudly rather than returning None.
- Required `git grep -c 'message_by_id' HEAD -- src tests | awk -F: '{s+=$NF} END{print s+0}'`: **0**.

Source-first NRA census before edit: 1,064 Core production/tests/tools, paired Toad production and pinned audit dependency modules; zero parse omissions, 888 declarations, 3,595 reference sites. Exactly one message_by_id declaration and 20 attribute calls. Dynamic aliases are not established by syntax. Evidence: .scratch/p105/before.json.

Verification:
- Requested `test_large_transition_reopens_and_exports_without_a_second_parser_limit`: **1 passed** on exact head atop07cf150dd. It failed on old main because the old public reference reader omitted claim envelopes; P102 fixes that through its existing snapshot owner. No fallback or weakening added.
- Prior bounded source check: six other existing wire/tool checks passed; no wider native-suite claim.
- Exact-head ratchet against07cf150dd: **0**, source -9, WireLog class-size -9.
- Whitespace clean. Initial F401/F821 check reported inherited unused imports and its default Python3.11 target rejecting existing PEP695 syntax; no new lint pass is claimed.

No shared installation, saved-session mutation, restart or UNKNOWN replay. Codex owns paired installation and installed P102 acceptance.
