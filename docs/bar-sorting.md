# Bar sorting

Owner: core-route-counts. Integration: codex. Coordination: slop-census.

## Required behavior

Core owns channel and member ordering. A chosen clock sorts newest first;
member ties use creation time then case-folded name. Channel pins partition the
list, builtins retain declaration order within each partition, and ordinary
channels sort by name or chosen clock. Name ordering must not gain a creation
tie-break. Member pins remain channel-scoped. Relationship groups retain their
independent orders, including mixed-order views.

Persisted scalar names remain unchanged. FieldCodec decodes them once into the
declared ordering member. UI controls must not recover behavior from these names.

## Source evidence before changes

At Core af047a76, `display_order.py:26-72` chooses timestamps through enum-keyed
maps and tests NAME separately. `presentation.py:611` consumes the key;
`relationships.py:741-748` separately recognizes LAST_MESSAGE to acquire its
clock source. Catalog, Channel, relationship rows, tools and SidebarModel carry
those choices.

At Toad a04e0ee4f, `widgets/session_sort.py:57-92` converts enum members to strings,
checks a second choice map, dispatches extras by string and supplies a no-op
`choose_extra`. Lines 114-152 duplicate route selection. RelationshipSort retains
mixed-order meaning separately from its default display value.

NRA's existing source index scanned 320 Core and 281 Toad modules before edits
(inferred package context included); exact declaration/method/base rows are in
`~/.cache/agent-scratch/core-bar-sorting/{core,toad}-before.json`.
This is syntax evidence, not equivalence proof or complete dynamic binding.
Runtime family registration and Textual handlers require application checks.

## Ownership and deletions

- DisplayOrder supplies labels. ThreadSort and ChannelSort are distinct declared
  families, not interchangeable because their required inputs and tie-breaks differ.
- Each member supplies its clock/order fields. Shared parent code retains pin,
  builtin and tie-break behavior. RecentChannelSort owns the timestamp-channel
  algorithm; NameChannelSort does not inherit its creation tie-break.
- FieldCodec's existing declared-class representation preserves persisted scalar
  names; no new codec or compatibility aliases.
- Every catalog, tool, relationship and sidebar consumer takes the declared type.
  LastMessageThreadSort supplies its own sent-clock requirement, deleting the
  caller's concrete-mode decision.
- Toad sorting uses the existing FormBridge/ObjectState form machinery and typed
  choices; visibility edits borrow the existing saved-settings state. No parallel
  sorting implementation or settings store.

## Delivery checklist

- [x] Preserve unfinished native/route source on published branches.
- [x] Core declaration-owned ordering and complete production-consumer migration.
- [ ] Toad typed form/control and route-consumer migration.
- [x] Focused Core sanity checks: 61 passed in 6.50 s (channels, catalog,
  retirement and relationship families), pinned CPUs 4-9; after-search pending.
- [ ] Paired clean runtime, real click/change/reopen/update journey, retained selection.
- [ ] Review and installed delivery through integration owner.

Scratch owner: core-route-counts; purpose: source evidence and focused acceptance;
path: `/home/ts/.cache/agent-scratch/core-bar-sorting`. Retain concise results;
remove the downloaded paper and large generated output after delivery.
