# W6 stopped declaration carry — source checkpoint

Base: actual main `a38c06de24315fa868de45786e01fefadb0aaef1`.
Reviewed target producer: Core627 `b7e688292074a854870d7ca78397111c1dae1c7b`.
Draft: https://github.com/OpenHCSDev/agent-comms/pull/663.

## Owner and deletion

`NativeSchemaDeclaration` owns authentic source/target DDL, writable fields and
metadata, derived from `TypedTable`/`CoordinatorTable` membership. It now accepts
the bounded `(9,3,3,6)` → `(10,3,3,6)` relation only with unchanged old
coordination DDL/fields, native/binding/response declarations and additive journal
membership. The existing `SchemaMeta` declaration owns the changed release row.
No hand-maintained annotation table roster was added.

`carry_coordination` consumes that relation. It replaces the old whole-membership
equality refusal with exact original-member inclusion. Existing tables remain
in place unless their declared DDL or metadata row changes; new members are
created empty. Changed tables retain rowids through declaration-derived fields.
Every original fact/generated value, physical row identity, unrelated object
and autoincrement allocation is compared after the carry. The previous sequence
capture incorrectly looked in `inventory`, which intentionally excludes SQLite
internal tables; sequence presence now comes from the actual schema owner.

`NativeSchemaCarryPlan` and `CarryNativeRuntimeInstallation` retain the original
stopped-custody lifetime, separate candidate, exact original hashes and preimages,
destination-filesystem staging and immutable matched target declaration. Native
input/journal/proof/fork facts are never created or reinterpreted by coordination
carry. Pending/hot-store refusal is unchanged.

## Consumers and limits

Original NRA `audit.findings.Package` / `FunctionFacts` before-source evidence is
`BEFORE.json`: source316 / tools52 / tests368 parsed at main, and source324 /
tools52 / tests369 at the actual627 target; zero parse omissions. Static calls and
metaclass membership still need semantic reading; this is not dynamic resolution
proof. The read consumers are ordinary `CoordinationStore`/`CoordinationSession`
and `SchemaMeta.require_current`; all remain byte-identical to main and strict.
The operator consumers are the existing stopped publisher member, candidate
review/install and `native_schema_carry_controls.run`; no second migration path
or ordinary reader conversion exists.

The existing private stopped control now distinguishes Native5→6 historical
request conversion (which still requires original selected-summary rows) from
9→10 coordination carry (which must not invent historical summary evidence).
It checks that the target ordinary metadata reader refuses the old source before
installation and accepts the actual target afterward. Existing original hash,
preimage, companion, candidate and journal preservation controls remain in place.

This checkpoint is SOURCE ONLY. Changed tools parse and `git diff --check` passes.
No product import, SQLite/store/control execution, prefix write, old corpus borrow,
archive, native job, provider request, public operation or replay has occurred.
Actual preserved9→10 installed acceptance remains unqualified. It requires a
future explicitly granted genuine schema9 declaration donor and a schema10
installed target, using the existing stopped control/custody. Einstein retains
W6/GUI and its source/helper/native consumers; this PR changes only the three
existing outside-src operator-family files plus evidence.

`AFTER.json` records the final owner/caller AST at determining137be40c (same
316/52/368 roots, zero omissions). `CHECKPOINT.json` records exact tool hashes
and the unchanged production/package roots. An absent original journal remains
absent; the existing candidate-tamper control uses an actually present declared
store, rather than requiring a made-up journal. No historical evidence is seeded.
