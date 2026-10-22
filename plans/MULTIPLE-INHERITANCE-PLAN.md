# agent-comms: eliminate restated facts through multiple inheritance

Consolidated 2026-10-22 against `main` at `48c877809`. This is the current plan; the earlier audits remain evidence about their pinned heads, not additional versions of the instructions. Current phase dependencies and completion evidence belong to the `MultipleInheritancePhase` members in `src/agent_comms/plan_phases.py`. Read those declarations and the affected source before assigning work; a historical candidate list is not a current backlog.

## Outcome and ownership

Declare each required fact once and derive its uses. Shared behavior belongs on its existing parent; overlapping capabilities compose through multiple inheritance and the MRO. Migrate the complete related family and its consumers, then delete the replaced flags, branches, rosters, field declarations and implementations. A marker class beside the old consumer decision does not complete the change.

The implementation mechanisms already exist:

- `DeclaredFamily.members_with` derives family membership from inherited capabilities. Delete hand-maintained member lists.
- `MroDispatch` and `@handles` select behavior through the value's MRO. Put the behavior on its owner instead of rediscovering a nominal type in each consumer.
- `FieldCodec` owns field decoding and encoding. Extend its declared capabilities in `field_codec.py`; never add another codec or subclass it elsewhere.
- `TypedTable` owns columns, constraints, schema reconciliation and derived trigger SQL. Table capabilities compose there.
- `LifecycleState` and `lifecycle_capability.py` own shared state behavior. For example, `Terminal.successors()` already supplies the empty successor set; subclasses do not restate it.

The virtual-bus plan shares these owners. Its `Addressed` capability owns `sender` and `target`; this plan uses it rather than declaring a second field group. Bus byte positions and obsolete `wire_*` fields are deleted by that plan, not preserved in new mixins here.

## Rules that decide a change

- **A demonstrated restatement is slop regardless of its copy count.** Two copies do not earn an exemption. Share a required fact through its owner; retain separate facts when their required answers or independent evolution differ. Current syntactic similarity alone does not establish a shared requirement.
- **Recognize the defect before the complete repair is known.** Name the restated fact and its defective encoding. Then use AST evidence and semantic reading to trace declarations, writes, decisions, imports, inheritance and all consumers across the relevant production and dependency roots before structural edits. Report parse omissions and unresolved dynamic relationships.
- **Capabilities carry the behavior they replace.** Moving `flag = True` into an empty base while changing `if x.flag` to `isinstance(x, Capability)` only relocates the decision. Move that decision onto the owning type or its existing dispatch mechanism and migrate every consumer. Siblings retain only irreducible differences.
- **Delete the competing authority in the same coherent change.** Rewire callers onto the owner; a broken caller does not justify restoring the copy. No compatibility aliases, alternate codecs, legacy readers or parallel caches.
- **Judge ownership, not line budgets.** Report source changes as measurements. Neither a net reduction nor a net increase establishes whether a fact is correctly owned. New classes must carry existing work and eliminate a restated fact or duplicated mechanism.
- **Choose the transformation from the source.** Use a codemod for a mechanical family-wide rewrite when it removes repetitive editing; its reader changes and deletions belong in the same change. A codemod cannot decide whether two implementations mean the same thing. One-shot transformation tools are deleted before delivery.
- **Preserve required behavior and external contracts.** Do not wrap external AST nodes, SDK types, `typing` objects or deliberately open external shapes merely to make a local scanner quiet. Distinguish genuine external-boundary handling from switches on types we own.
- **Confirm the coherent change last.** Run the focused checks relevant to the changed behavior and exercise the actual affected installed entry path. Do not run a full suite per batch or use repeated test runs to choose the design. Merge and installation follow the current commander rules.

## Storage decision

Preserve durable table and column layouts during declaration factoring. `TypedTable` compares stored schema text, so a field-group change must retain the affected table/column DDL, including column order, byte for byte. Preserve required record meanings and external wire contracts. Share a declaration only where it represents the same fact and preserves that layout; a layout change requires its own explicit migration decision.

**Handwritten triggers are replaced, not reproduced byte for byte.** The 2026-10-21 correction superseded the original instruction to preserve their SQL text. Copying each trigger's message and whitespace into capability parameters would merely move the same SQL. A trigger holds no data: derive its guard from table and column declarations through `TypedTable`, and use the installer's one-shot cutover mechanism to drop the old triggers and create the derived ones. Preserve their required constraints and superclass promises; verify the stored result against the derived declaration. Delete the cutover tool after the affected stores have been migrated. Reuse already delivered trigger capabilities and migration work.

## Work by owning family

The phase names below identify the existing declarations in `plan_phases.py`; that family owns their dependency graph and completion evidence. Recheck the current source before dispatch so delivered work is not implemented again.

### Batch 1: capabilities and shared implementations

**1A, `LifecycleFlagsPhase`.** Reuse `lifecycle_capability.py` across assignment, obligation, execution, goal and input lifecycles. The original candidates were `terminal`, `failed`, `successful`, `pending`, `active`, `executing`, `unresolved`, `deferred`, `complete` and `completed`. Trace each required meaning and its readers; similarly spelled flags need not answer the same question. Move shared transition behavior into `LifecycleState` and its capabilities. Delete flag defaults, overrides and repeated answers already supplied by a parent.

**1B, `CodecFlagsPhase`.** Extend `FieldCodec`'s existing field representations and dispatch. The original candidates were `accepts_null`, `opaque`, `external_json_shape`, `strict_fields` and `accepts_prompt`. Reuse the capabilities that already own these decisions; remove remaining competing readers and declarations. A remaining wire tag belongs to the existing declared wire family, not another handwritten tag roster.

**1C, `SmallFamilyFlagsPhase`.** Move command and other family decisions onto their existing owners. The phase names `multiple_targets`, `preengagement_target`, `notification_changed` and `input_uncertain`; the latter was originally declared eight times and read nowhere. Delete unused facts outright. `GoalDisplay.visible` has an explicit retained decision recorded in the phase declaration; this plan does not silently broaden that decision to unrelated visibility flags.

**1D, `TriggersPhase`.** Derive table guards through `TypedTable` capabilities. The historical surfaces were `cohort_schema.py`, `coordination_response.py`, `coordination_tables/*`, native input/context tables and sidecars. Reuse the delivered `AppendOnly` and other table capabilities; remove any remaining hand-written trigger authority. Apply the storage decision above rather than recreating old SQL through a new formatter.

**1E, `DuplicatesPhase`.** The phase declaration retains the eight exact-shape candidate groups and their source paths: constructors, owner identity, computed triggers, wire-member lookup, schema assertions, references and annotation disclosure. Read each group semantically; share only the required answers that agree across supported evolution. Trace the additional near-shape constructor, parser and `apply` candidates at the historical audit head before retaining them as work. A shape match is a lead, not a proof that two facts are interchangeable.

### Batch 2: field declarations and table capabilities

**`FieldDirectivesPhase`.** One owner handles related field declarations and their readers together. The directive classes already exist in `FieldCodec` and `TypedTable`; extend them rather than introducing another metadata vocabulary. Fields attach the declared directives through `Annotated`.

Factor surviving shared field requirements through their owners. The phase names `TurnItem`, `ClaimScoped`, `SingletonRow`, `SchemaMeta`, `Revisioned`, `ExecutionKeyed` and `WithoutRowid`. Check current declarations and members before adding any class. Table factoring must preserve the stored table/column DDL described above.

Do not add an `OwnerIncarnation` field mixin merely to preserve copies of registry identity: that deletion belongs to `plans/REMAINING-SLOP-PLAN.md`. The virtual bus owns `Addressed` and deletes obsolete bus byte spans and wire positions. Coordinate shared field lines with those owners; do not factor fields that another plan removes.

### Batch 3: consumers of owned types

**`OwnTypeChecksPhase`.** Once the owning families are coherent, migrate their consumers from `isinstance` decisions to owned methods or existing `@handles` dispatch. Trace the full behavior family, including its boundaries; replacing one branch while leaving sibling consumers to rediscover the same fact is unfinished work. External taxonomy handling remains at its proper boundary.

### Batch 4: deletion closure

**`InheritanceCleanupPhase`.** Remove obsolete capability shells, duplicated answers, former-flag branches and member rosters. Keep abstractions that own required shared behavior; collapse forwarding-only or empty distinctions that do not. Confirm the phase's parent-sharing evidence and inspect what it establishes rather than treating class counts as a design argument.

## Evidence and completion

The historical source audit is at `a40e7f9cd` (2026-10-21): 426 modules, 10,936 callables, zero parse omissions with Python 3.14. These are measurements of that head:

| Historical finding | What the corrected audit established |
|---|---|
| Repeated class capability flags | 85 lifecycle flag lines, 44 codec flag lines, and 167 small-family flag lines were candidate declarations to trace, not guaranteed deletions. |
| Handwritten triggers | 29 methods, 930 lines and 80 triggers; moving literal SQL bodies into a parent would not remove their separate declarations. The trigger rewrite decision above addresses that cause. |
| Repeated behavior | After excluding literal-return bodies, eight exact-shape groups had 91 candidate removable lines, 58 then classified as certain; three near-shape groups required further tracing. |
| Earlier inflated duplicate claim | The 1,518-line estimate erased literals and conflated distinct SQL requirements. It is rejected, along with the original claim that behavior was already shared well. |

The original source witnesses, full trigger inventory and candidate line anchors remain in Git history at `04df26b76`. Use them to locate historical evidence, then trace current source. Do not copy those inventories into a second live roster.

A phase is complete only when its declared evidence holds on `origin/main`. Delivery must also explain the before/after ownership and consumers, show that replaced decisions and declarations are gone, preserve the required stored data and behavior, and exercise the affected application path. Report merged, installed and seen working as the distinct facts their evidence establishes. A passing scanner or a smaller diff does not establish those other facts.
