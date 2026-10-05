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

## Control API correction before execution

Parent found two incorrect new `SchemaMeta.require_current(db)` calls and a
`CoordinationSession.read()` yield incorrectly treated as a DB connection. Both
now borrow the existing `CoordinationStore.observing` resource, decode the actual
PRAGMA through `SQLiteUserVersion.read`, and pass its version to the original
metadata owner. The old-source refusal catches exactly `SchemaVersionError`.
No writable/initializing session or private chmod belongs to read acceptance.
The existing `rows` helper converts original SQL rows to cell tuples once;
otherwise the observing owner's SQLiteRow factory would compare unequal to
authentic declaration tuples. No row factory override or alternate codec exists.
The non-carried file hash snapshot and native membership partition use distinct
names; the source read also removed accidental local shadowing of those facts.
These defects were found and corrected at source, before any installed gate.
Both call arities match the parsed existing declaration; no runtime success is
claimed. `PROPOSED-OPERANDS.json` binds source540, target627/ab640 on334 after
Heis443 handback, unchanged canonical source seed, fresh authored/stopped roots,
exact original-control argv and evidence paths. Both actual purposes are pending.

## Current proposed target binding

The current proposal uses Core627 determining3aacd708 and retained normal wheel
0ed358e9 (355 assets), replacing only the old targeta59/ab640 tuple. Einstein's
published stopped-carry-current-main-target-operands.json, SHA334b6db6, owns the
new target source/wheel/inventory and proposed shared historical window.
CURRENT-TARGET-RELATION.json records this accepted source binding: every355
member matches its existing inventory's Git/local/ZIP source, including the
three forced assets. Exactly native_admission_epoch, tracked_turn and
transcript_outcomes differ from oldab640. All SQLite declarations, metadata
owners and native manifest/producers remain unchanged at source scope.

Original PROPOSED-OPERANDS.json stays byteexactb3af, recoverable at published9d6;
its old334 interpreter/ab640 entries are historical, not current authority.
The frozen88e8 tool hashes and existing control recipe remain unchanged. Future
argv uses the newly issued eligible target interpreter in place of original334,
then the same native_schema_carry_controls.py/base/--source-python540/
--stopped-original stopped-original9 arguments. Existing output paths and only
the newly authored stopped-copy root remain the same. No second seed or corpus
copy is proposed. The genuine9 source loan is independently whole closed;
a new source re-declaration read purpose is required for the actual control.

PUBLIC334CURRENT447 is excluded. Formerstyle22 is only a future candidate after
Bohr names its actual eligible floor and issues the exact target/source purposes.
Einstein remains sole package writer and overall handback owner; Mendel owns
only carry on the new stopped copy. Carry does not wait or extend the historical
GUI and does not acquire a native/provider/input/App permission. No donor/target
import, package/store access or carry operation ran in this source update.

## Carry-only purpose operands after historical05 return

CARRY-ONLY-OPERANDS.json is the literal future binding for NONLIVEstyle22,
replacing the historicalPUBLIC334 argv. It binds the reviewed3aac/0ed355 target,
original88e8 three tool hashes, and the existing15-file stopped-original9/base
and output paths. The manifest rechecks full modes, hashes and mtimes against
the original authored tree; its original inode identities remain unchanged.
Source declaration971104/seed2be7/source whole7f827 remain the original receipts.
The old8f7c source grant is closed, not inherited.

The effective target command is style22/bin/python -B followed by the existing
native_schema_carry_controls.py entry and original --source-python540 and
--stopped-original arguments. That original control alone invokes the unchanged
sourcePython native_schema_carry.py --declaration child. Inherited
PYTHONDONTWRITEBYTECODE=1/PYTHONPATH absent applies to both; no manual duplicate
source probe or seed is proposed.

Historical05 returned the actual1469/69/519 floor; closure is eligibility only.
Bohr must issue both new source540 re-declarationREAD and targetstyle22
READ/isolated carry scopes. Einstein alone stages Core355 and returns the whole
floor; Mendel only executes the carry against its own new stopped copy. No
interpreter was imported or used, no source/target package access occurred, and
no App/native/provider/input/oldW6 corpus or new copy is authorized here.
