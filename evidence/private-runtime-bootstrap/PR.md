## Fix

A fresh private bus could advertise committed initials while its coordinator had only the base tables, leaving recipients idle. Bootstrap now runs in the canonical fresh protocol issuer (including ordinary first-send initialization) and explicitly private owner launch, before marker visibility or worker spawn.

Existing schema declarations carry an installation capability; MutationStore discovers those owners through the existing TypedTable family and invokes their canonical installers. There is no second table/function roster, schema converter, codec, or reader-triggered repair. Existing schema drift remains an error.

Removed manual schema installation from ACP delivery fixtures and the fixture used by the actual selected-native regression. Added real detached native peer delivery/restart coverage for both fresh protocol creation and a base-only coordinator, plus a drift/refusal check.

## Validation

- Real detached native peer delivery/restart passed for fresh protocol and base-only coordinator; drift refusal passed (3 tests,24.79s).
- Installed-wheel verification: fresh peer/restart, drift refusal, and existing native coding-tools regression with manual schema setup removed all passed (3 tests,15.74s).
- Focused source validation covers the migrated foreground bootstrap caller.
- Entrypoint/ACP run:39 passed; unchanged main reproduces the same eight remaining ACP failures (cursor/fake-model expectations and changed error text). Receipts in evidence/private-runtime-bootstrap.
- Production:57 lines added/17 deleted. Removed the foreground four-function roster and16 lines of manual schema test setup. Added the missing lifecycle connection and real native startup regression.

Parent owns live database repair and exact pending message151 verification. No live state changed; CI deferred.
