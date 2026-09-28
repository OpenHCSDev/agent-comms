# T2 paired checkpoint

Shared update decoding now covers queue, cursor, coordination, goals, compaction progress/commit/publication, transcript snapshots, input-ledger invalidation and MCP receipts. The native MCP JSON receipt is decoded once into declaration-owned server state and call policy; its old schema walk and state roster were deleted. Transcript snapshots are unconditional on this paired protocol; own snapshot/image capability negotiation was removed. Runtime presentation rebinds typed session scope rather than probing fields.

Verification: focused family round-trip/strict boundary test: 1 passed (pytest -o addopts=""). Default repository coverage configuration failed its aggregate 85% threshold when running just this test; no full suite pass claimed. Fresh ACP producer process through SDK JSON RPC into mounted actual Toad passed turn start, stale settlement rejection, matching settlement and compaction start/progress/end rendering using the installed round2-final Python with candidate source paths. No provider call or live-root mutation.

Remaining: typed request decoding, copied UI-message deletion, dead replay fallback deletion and all meaningful caller/test migration; queue/cursor freshness and copied converted-history/native acceptance. These paired drafts are not complete or ready to install. Parent266 owns startup preparation/readiness; this batch changes only backend MCP receipt parsing and preserves the parent startup surface.

## TR0 sync and turn caller closure

Synced core main fc5dd38a (268) and Toad integration e6c5227 (117 merged into round2-l0a-callers, not main). Preserved ViewportPresentation windows/anchors and shared per-class ratchet. Removed duplicate Toad TurnStarted/TurnSettled classes; actual turn facts travel in CommsUpdated with immutable local sequence/session/agent envelope. Migrated all source and pilot turn constructors. Remaining historical pilot protocols still need the separate snapshot/compaction/MCP migration noted above.

Built both candidate wheels into owned .artifacts/paired-installed. Dependency fallback points at installed final runtime site-packages; candidate packages are loaded from their isolated wheel installation, with no candidate source override. Actual mounted producer/consumer turn+compaction pilot and ACP validator isolation/order/retirement pilot both passed. Initial guard attempt used a tooling venv without the new agent-comms-ratchet executable; installed environment resolves that. Parent266/267 startup work is not duplicated.

Installed focused family + shared ratchet regression tests: 20 passed, 11.76s. One pytest configuration warning: asyncio plugin not installed for these synchronous tests.
