# B5 / S7-R6: remove disconnected bridge implementations

Deleted ordinary_delivery_bridge.py (295 source lines), transcript_route_legacy.py
(220), and their exclusive tests (138+245 lines). Repository caller/import scan
found no production consumer; canonical cohort dispatch and recorded transcript
routes already own those behaviors. No replacement store or compatibility import.
Original saved files are preserved.

Current-path regression batch:45passed,1failed on a stale package-export call in
test_transcript_legacy_integration. Migrated that existing caller to its already
imported routing.TurnRouting; all5 cases in that file now pass (46 distinct cases
covered across the combined selection). Failed receipt retained.

Built candidate wheel, installed in an owned target directory and mounted current
Toad DM views against actual live history with that candidate core. Both owner
views render recorded channel receipts, no messages sent. Parent owns publication
and installed runtime activation. CI deferred.

plans/refactor-deletion-closure-20260928.md maps every existing plan and remaining
cleanup batch to its owner/PR status. This PR closes B5 only; B1–B4 remain active.
