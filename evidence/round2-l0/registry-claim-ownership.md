# Registry identity ownership and archival read closure

Current source: parent229 after d1511fa.

The repeated whole-registry claim check in Messaging is deleted. Both canonical
initial publication and claim publication now ask RegistrySnapshot to establish
unambiguous ownership on the exact snapshot used to resolve the message.
The claim path uses that same snapshot for validation and message construction.
No second identity registry or disk format was introduced.

Actual retained-data check prevented an incorrect broader deletion: archive
source-0 contains helper-bot and nominal-refactor-advisor-2 with created_at=0.
Rejecting every such document during decode would hide original history.
Archival reads therefore remain valid; those identities cannot authorize a
publication. The current staged registry has 104 distinct creation identities.
Current + both archived registries reopen with 104/104/7 threads. No original
source was modified and no creation identity was rewritten.

Verification: 3 focused real-store tests pass, including saved/reopened colliding
identities rejected before claim append, read-only archival identity retention,
and registration collision with legitimate subsequent claim release. Broader
registry/envelope run: 28 pass, 2 existing fixture failures, 1 deselected. The
failures are old Publisher.publish use and a fixture creating bus.jsonl as0644;
Nietzsche owns their closure in248. The existing goal-family fixture separately
requires BlockedGoal.block_reason; that correction also belongs to248.

Remaining global L0 gap: native_source_cursor/proven_source_coverage still select
checkpoint pages versus whole-bus capped reads. Fresh Publisher initialization
currently does not create a checkpoint. Parent owns making the canonical
bootstrap produce the current checkpoint and deleting the alternate runtime
reader, with fresh-root, retained-history and native cursor acceptance. Existing
external vendor paths such as openai/internal/shims.mjs are protocol paths, not
identifiers/comments or a compatibility implementation to delete.
