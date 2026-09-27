# Additive roster and channel restoration

The existing ThreadRegistry now owns `restore_stopped(snapshot, names)`.
The existing ChannelCatalog owns `restore_missing(source)`.
No second registry, live route, delivery ledger or identity allocator is added.

Registry restoration preflights the entire selected identity set, preserves
existing declarations/epochs, imports missing saved-session declarations as
stopped (or retains archived status), clears old PIDs/active turns, and restores
only aliases whose target incarnation matches. It writes through the normal
registry writer and private durability guard. It does not copy bus rows,
read markers, coordinator attempts, native inputs or pending work.

Channel restoration preserves current preferences and restores missing tag,
named-channel and saved-view metadata through the catalog's normal writer.
A mixed parent cycle is refused. These methods are metadata restoration,
not completed history pagination, execution recovery, or session validation.

## Evidence

- 46 focused tests passed: roster restoration, supervised cutover, channels,
  registry revisions. Private-bus test checks unchanged logs and current owner
  admission epochs, no replayed inbox, alias resolution and idempotency.
- Actual public/current snapshots were exercised in a disposable private root:
  96 unambiguous missing identities restore; resulting roster has 103 entries
  and includes #nra and #openhcs. Every selected saved session path exists.
- Two old records have a colliding creation timestamp and are not selected.
  Three current names have newer incarnations and remain unchanged. Their
  historical identities remain in the source; no replacement is synthesized.
- NRA package-context scan exceeded its deadline while parsing. The preserved
  report is incomplete, not a clean ownership audit. `recipe.json` uses
  declaration-targeted member insertion on the existing stores, simulated over
  the full package and applied with NRA's revision-checked transactional writer.
  The new algorithms are authored behavior, without an equivalence claim.
- Ruff and git diff checks pass. CI is deferred by the owner.

Activation uses the additive methods on the active root. Existing installed
clients already consume this registry/catalog schema and need no owner restart.
The live operator keeps previews, backups and receipts under local state.
All older message history, colliding historical records, and missing native
session provenance still need integrated history restoration; this is not a
claim that the full migration is finished.
