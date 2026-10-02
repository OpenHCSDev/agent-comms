# TR0 shared ratchet — ready for source integration

PR254, refactor/tr0-packaged-ratchet-20260928. Reconciled merged core229/main
c8b0d226 without production conflicts. Parent controls final install/pin.

Completed original R0/TR0 package/caller deletion:
- tools/debt_ratchet.py moved into installed agent_comms.debt_ratchet; old script gone.
- agent-comms-ratchet requires --root/--base/--head; changed-file union, source-root
  exclusion and move/deletion accounting retained.
- Existing DeclaredFamily owns Measure discovery. Comparison+FieldCodec own report
  projection; no copied roster/report mirror. Packaged move adds zero syntactic debt.
- Core workflow calls the installed console. Workflows are manual only; CI deferred,
  no branch protection/required-check changes. Canonical docs record this override.

Acceptance: 11 installed actual-Git/console cases passed on e320080 + current229
source; merged-main reconciliation changed no ratchet source. Receipts retained in
ratchet-integrated-tests.log and ratchet-final-tests.log. own-ratchet-final.json
records zero deltas for the package move. Config warning in integrated test run is
only disabled pytest-asyncio plugin, not a missing executed test.

Paired Toad117 owns committed pin/lock, installed collector and UI suite triage.
Installed real Toad→ACP→owner→Pi loopback queue/reattach/channel acceptance passed
there against e320080 and prepared native-session-entry-store package. No paid
provider, live deployment/restart, editable shared package, or new native copy.

No remaining blocker for254. Toad's broader discovered UI failures are explicitly
owned followup in117 evidence/tr0/HANDOFF.md, not a full-suite/CI merge gate.
