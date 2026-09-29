## Ready: S14 registration authority closure

**214 production lines deleted; 224 added across nine existing modules.**

- Removes registration's repeated live read, turn claim and compaction validity chains. Reuses RegistryOwner, actual process/thread/owner identities and declaration-owned state.
- Existing rule family distinguishes fresh identity+goal claims from sends requiring the captured active turn. Admission claims allow unrelated metadata and a finished captured turn; owner-generation claims still require exact declaration equality.
- Existing OwnerCompactionAttestation owns decode/bounds and exact GoalRevision identity. Same durable format, lock-through-write, maintenance fence and inherited-child authority. No new codec/store/compatibility path; no UNKNOWN reset/replay.

### Actual evidence

- 39 real registry/process/lock/turn cases pass.
- 17 admission/selected lifecycle cases pass; six new claim cases all pass after correcting one fixture that canonical Thread validation rejected before the intended admission check. RED retained.
- **Noneditable installed + actual d396 native: 2 pass / 25.16s**. Retained private history → ACP prompt → selected native summary → durable commit → original delivered once; also no-goal exact-turn compaction. Local controlled provider transport, no paid calls.
- 18 installed strict boundary cases pass, including bool/float coercion refusal, incomplete goal identity, native field types.
- Ratchet no increases: -14 type checks, -10 long chains, -76 per-file chain terms, -19 foreign absence probes. Ruff/diff pass.

Complete evidence, failure attribution, actual import paths and cleanup in `evidence/s14-registration/README.md`. Production checkpoint9ff2694c; later changes only tests/docs/receipts. Parent retains live activation/UI acceptance. No claim this worker changed the live installation.

Based main1dbb318d including353/354/355. Disjoint parent native_startup/watchdog and Carver356 diagnostics. Applied current NRA/refactor-audit IDEN-1/3/8, BOUND-1/2, IMPL-10/14. No global scan/new agents. CI deferred; no additional proof gate proposed.
