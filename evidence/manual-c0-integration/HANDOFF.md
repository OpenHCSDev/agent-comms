# D1 manual ownership integrated with current C0

Parent integration branch: integration/manual-owner-20260928, based on main after PR182. Merges PR181's complete implementation and retained audit/evidence. The sole textual conflict was the manual bridge test hook: keep ManualCompaction.run from D1 and the C0 goals component as the wait-release owner. Production bridge retained current C0 accesses automatically.

Current combined acceptance: local.txt 28 passed, including actual local Python RPC child/Node preflight, cancellation/timeout cleanup, bridge settlement and native refusal/launcher contracts; acp.txt 5 passed,90 deselected, preserving ACP manual lifecycle. Both commands completed exit0. Ruff I/F and source/test diff checks passed. Worker D1 ownership/boundary evidence remains in evidence/post-pr95-debt. No provider/model calls or live data mutation.

This integration is committed, not installed. Current live runtime remains runtime-c0-20260928. Next: publish/merge the integrated PR181 change and install through the existing isolated runtime path, retaining default adaptive compaction and the canonical-native manual admission guard. D2-D4 implementation is separately owned by Darwin; S7 bus by Pascal. CI deferred.
