# TL0A paired MCP package deletion

Paired with Toad118/9f3f008 (Toad107/current core9e3dc3c base).
This branch changes only the MCP inventory producer, its obsolete test fixtures,
and documentation. Existing parent native package preparation remains authoritative.

- Delete the inventory capability/positive-decisions claim; Toad now binds its
  inventory and decision CLI to Comms' verified prepared package instead.
- Delete the historical two-version reconstruction test and both copied old
  modules. The retained call-grants test already proves denial between snapshot
  and locked write is rejected and reapproval cannot resurrect the grant.
- Delete old README instructions requiring a configurable CLI and capability
  token. Current CLI test guards the removed field's absence.

Source +0/-4, tests +1/-170 (including old source fixtures), docs +7/-15.
No production ledger writer, trust/config format, receiver or external ACP/Pi
format changes. No durable/runtime store conversion or reset needed.

## Evidence

Five local Node tests pass, no skips, 9.44s: real spawned CLI current inventory,
redaction/trust/refusal; actual local MCP fixture server call grants/revocation;
stale-approval locking race and reapproval; atomic writer validation.
Dependencies are read-only links to the already prepared Darwin0d7 package;
all writable fixtures are in this owned worktree. No provider/live calls.
Initial missing peer package fixture setup is recorded, corrected with a
read-only local dependency link, not an installation into shared package.

Toad118 already passed installed MainScreen and real pinned PTY actions using
that exact package. This source removes one unused output field; parent must
rebuild/pin the native package after integrating the source before reporting
package-side activation. No live or prepared package bytes were modified here.
Parent owns package preparation and live activation. CI deferred.
