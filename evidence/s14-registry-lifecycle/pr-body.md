## Ready: S14 registry lifecycle closure

**133 production lines deleted; 250 added across seven modules.**

- Removes the optional previous/status/new_owner operation bundle and RegistryDocument.apply_registration; all callers migrated.
- Public ABC and initial/update/explicit-restart declarations own epoch changes, maintenance admission, publication fencing and imported-turn revocation. Shared implementation stays on the parent.
- Actual ThreadPublicationIdentity replaces publication field comparisons. Existing TurnLeaseFence owns exact release after alias resolution; reused IDs/replacement incarnations cannot release other turns. Restoration derives aliases from matching retained identities.
- Durable schema, epoch domains, lock ordering, UNKNOWN handling and no-replay behavior remain intact. No new registry/codec, compatibility alias or old implementation.

### Real evidence

- 44 registry/process/restoration/maintenance checks pass; seven new transition/alias/idle-fence cases and four owner cases pass.
- Five actual owner-restart/publication-fence checks pass after correcting the omitted package fixture variable.
- **Noneditable installed full native/ACP journey: 1 pass / 31.24s.** Both owners save history through ACP → native channel reply and automatic sender IGNORE → guarded restart preserves exact session bytes with new process/admission identities → fresh attachment and one explicit input delivered exactly once. Five localhost provider calls; real core, native, journals, sockets and processes.
- Ratchet no increases: -37 chain terms, -7 long chains, -7 foreign-state probes. Ruff/diff pass.

Complete receipt and retained failures: `evidence/s14-registry-lifecycle/README.md`. An initial new test wrongly assumed selected-delivery journals always populate Thread.session_file; it now seeds real canonical sessions through normal ACP. No product change or assertion weakening to fit the test.

357 is merged; normal current-main integration includes359 at12bd75ca. Product checkpointf8166be7; subsequent changes are integration and tests/receipts. Parent owns installation/live UI acceptance. Disjoint startup/watchdog and failure/T4 implementation. Latest NRA/refactor-audit patterns applied; no global scan/new agents. CI deferred; no unchanged matrix requested. Owned derivative installations cleaned.
