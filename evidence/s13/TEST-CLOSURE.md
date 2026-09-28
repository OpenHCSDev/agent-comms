# S13 remaining caller/test closure

Fixture migration covers46 files. Thread constructor/replacement sites now carry captured real ProcessIdentity, retain an existing identity, or use None for an unowned record. No synthetic birth constant, PID constructor adapter, or production compatibility was added. The foreign-owner compaction check launches a real different child and retires it in finally.

Tests were three-way merged against parent integration bb1892e so S9's deleted compaction-process helper and current publisher declarations stay deleted. The parent-deleted supervised-cutover test was excluded rather than restored. Queue tests test_acp_queue_contract.py and test_prompt_queue.py were untouched. Parent now owns test_acp_private_nk_delivery.py and test_read_ledger.py; their already-completed fixture changes are included per instruction, with parent resolving subsequent alias/read behavior overlap.

The reported missing test_goal_direct_interrupt helper import is removed: test_acp_owner_interrupt_followup.py binds the current canonical session and real identity itself. Existing followup assertions are retained, including actual RPC child acceptance, but their old direct_interrupt/pending-turn driver still requires migration to the new production followup API. This is not a passing runtime acceptance claim. A separate current canonical probe reproduced production goal_attempt_store_unavailable when fresh owner input arrives during a selected DM; routed to parent229 and Pascal234 (comments5873882575/5873882924). No retired dispatch path was restored.

## Local checks

- Current integrated required guard command:22 passed,2976 deselected,4.33s. See integrated-guards-after-test-merge.log. The missing helper collection error is fixed.
- Previous isolated consistent-source shard:91 passed,8 deselected. See identity-fixtures-core-consistent.log.
- Newer integration registry/lifecycle shard:63 passed,7 failed. See identity-fixtures-registry.log. Failures need current canonical bus/owner semantics, not pid constructor compatibility; parent subsequently owns read/alias cases.
- Goal shard:30 passed,8 failed due missing canonical bus marker in old ACP goal fixtures. See identity-fixtures-core-new.log. Assertions retained.
- Scoped Ruff imports/format and Python compilation passed.
- Failure logs are retained; earlier mixed-version/snapshot collection failures are not claimed as production failures. Integration tests run in our owned .artifacts/integration snapshot, not a shared worktree; no installs or live restarts.

A12 repeated-cancellation, inherited direct-parent launch, Windows, owner lifecycle and recovery behavior remain as previously published; no redundant rerun of completed child acceptance here. Full test caller closure remains open until current native followup seam and remaining bus fixtures are migrated.
