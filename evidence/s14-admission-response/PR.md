## S14 selected admission / response identity closure

**205 production lines deleted; 284 added.** Current-main-integrated source `e7c5a765`.

Replace admission identity chains, duplicated attempt token authority and scalar live-response witness with actual AssignmentBinding, OwnerGenerations, ProcessIdentity/ThreadIncarnation, lifecycle state and RegistryOwner. Message/receipt source identity is shared; canonical Message owns response envelope equality. Migrate every affected caller; no codec subclass, wire change or compatibility layer. Preserve exact source/receipt/current-owner/uncertainty fences and locked write custody.

Awareness belongs to **PR351**. My overlapping uncommitted awareness draft and TypedTable extension were withdrawn;350 does not edit either file. Historical intermediate receipts are labeled accordingly.

### Verified

- Noneditable installed lifecycle: **42 passed,7.54s**, real SQLite/claim/response paths, including PID birth reuse and ambiguous publication.
- Actual installed Pi selected-write → response publication → claim-release: **1 passed,3.44s**, controlled local provider only.
- Changed-path ratchet: **-95 chain terms, -10 foreign absence probes, -13 god-class excess; no positive measure**.
- Source and evidence: `evidence/s14-admission-response/README.md`.

Ready for parent source integration; parent owns live installation/acceptance. Full S14/T4 and final performance remain separate plan work. No CI hold.
