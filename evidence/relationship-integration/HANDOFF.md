# Canonical stores and reopened viewer fix deployed

PR169 replaces raw relationship/passive storage, deletes read-time duplicate projection and path/decoder adapters, and provides an explicit one-way version1 migration. PR167 removes full inbox materialization on human viewer reopen by using the existing route index with exact read-ledger membership. Both are merged and installed in runtime-relationship-cleanup-20260928. Paired Toad85 is merged/installed and stack pins/lock track core ee994e1, Toad837e2a2 and latest merged Textual4fa6a9c.

## Current evidence

-89 local relationship/migration/passive/contact cases pass;45 index/relationship/compaction seam cases pass.
-Current and installed Toad right-sidebar integration passes: real collaboration tools, mutual edits, sorting, preserved row identity, native/channel mounts, drafts and unavailable/copy behavior. Initial pilot failed because it re-added before the old row finished removal; Toad85 fixes the wait while keeping row identity assertion unchanged. Baseline deployed runtime also passed.
-Actual original-root migration applied under existing locks. All17relationships and2sort preferences survive; renamed original retained as history. All85passive rows remain byte-for-byte unchanged. Private pre-migration relationship file retained locally; no private note content published.
-Both existing owners restarted idle on same active bus,103identities. Fresh source49 caused triage/full, successful native read/bash and exact RELATIONSHIP_CLEANUP_OK reply50 in22.01seconds. No uncertain old inputs replayed.
-Installed historical UI acceptance is being concluded by Darwin after correcting unguarded multiprocessing test entry. Earlier errors are not silently called passes; authoritative handoff/receipt will distinguish harness failures and product behavior.

CI remains deferred. No new provider route/model and no additional agents. This completes the store/index implementation and deployment, not the entire still-active refactor goal.
