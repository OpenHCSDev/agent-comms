# Current T2 integration remainder

Production migration/deletion is complete in the paired published branch. Parent integrates/installs; Carver owns the combined Toad integration. No T2 runtime-format reset, source-history rewrite, converter restoration, CI gate or automatic input replay.

Concrete reported limitations:
- Four Toad view classes still exceed their previous lexical ClassSize count despite deletion of the old parsers/reducers/message classes. Core per-class ratchet has no increases. Keep this explicit in integration review.
- Existing Textual70-column reply-route layout recursion reproduces on installed live baseline. T2 does not claim to fix that separate layout defect.

Actual final receipt updates belong in FINAL-ACCEPTANCE.md; earlier full-suite completion/obsolete module reactivation is not a merge gate.
