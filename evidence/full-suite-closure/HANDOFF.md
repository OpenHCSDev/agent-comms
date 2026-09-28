# Combined full-suite closure — in progress

Branch starts at2f445bd and includes parent9b01741. Parent's
SummaryState.require_commit_reservation restoration/guard is retained.

First serial full run: pytest -q -o addopts='' --maxfail=12,
55-second command bound. 12 failed,35 passed in10.67s. Exact failing node IDs
are in first-failures.log. All first failures are test_acp.py fixtures using
unconfigured canonical roots or retired text/public-drain assumptions.

Current code-bearing checkpoint:
- Canonical owner fixtures construct the current marker/root/package configuration.
- Existing goal/provider/session/relay behavior assertions are retained for execution
  on current owners; implementation failures remain visible.
- Seven tests removed: five arbitrary text-executable launch/cancellation/error
  cases, the disconnected public inbox steering case, and automatic adoption of
  a reopened unused goal grant. These paths were deleted by L0. Native preflight,
  exact native start/UNKNOWN, owner socket/steering and explicit Retry/no replay
  acceptance already lives in current native/input/goal tests.

No full-suite pass claimed yet. Continuing serial bounded failure batches and
publishing fixes. Parent owns reservation-method source and D22/installed gates;
243/244 own native producer/history closures, Cicero owns view_unread scaling.
No T2 edits. No live state, installed package or paid provider changes.
