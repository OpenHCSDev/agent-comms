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
- ThreadSort and ChannelSort now own channel-scoped persistence as well; the
  two UI `store_order` forwarding overrides and their abstract dispatch are deleted.
- RelationshipGroup declarations own key/title/empty text/entry supply. The
  SortableRelationshipGroup parent owns saved ordering, defaults, selection and
  sorting. Snapshot supply derives from the existing family; the UI derives its
  editable groups from snapshot capabilities. The Core membership/default/
  constructor rosters, Toad GROUPS/EMPTY maps and duplicate presentation protocols
  are deleted. Before this change a new group required at least five outside
  answers; now its one declaration supplies them all. A crossing-capability check
  composes inbound supply and selectable sorting through two parents, without
  repeating either algorithm.
- MutualThreadRelation owns entry annotation/merging for explicit and goal-derived
  contacts, deleting the repeated implementations in contacts and snapshot supply.
  Each relation retains its own relevance, source and goal-link behavior.
- The existing family metaclass preserves the root's affix across slots-dataclass
  recreation, rather than adding duplicate explicit names to each group.

After evidence: `display_order.py` supplies both shared keys and persistence;
`relationships.py` contains the nominal group declarations and shared algorithms.
Search `GROUPS|EMPTY|store_order|group.key in|\\{\"children\"|\"children\":`
in the production sorting/group consumers finds none of the replaced authorities.
The final NRA index covers 320 Core / 280 Toad modules (one duplicate protocol
module deleted), 9,198 / 6,812 targets. No parse omissions were reported; dynamic
registry/handler binding still requires the real application.

## Delivery checklist

- [x] Preserve unfinished native/route source on published branches.
- [x] Core declaration-owned ordering and complete production-consumer migration.
- [x] Toad typed form/control and route-consumer migration.
- [x] Focused Core sanity checks: 67 passed in 13.30 s (declared families,
  channels, catalog, retirement, relationships). That command exited 1 solely
  because its targeted subset did not meet the repository-wide coverage minimum;
  no full-package coverage claim. Pinned CPUs 4-9.
- [x] Prior paired candidate in a separate runtime: real st/Xvfb saved-history
  startup; independent channel and member click/change/persist/reopen, application
  restart, mixed relationship ordering/change/reopen, live status/row updates,
  visibility editing/persistence, retained selected thread/root and unsent draft.
  Original live sort preferences restored. No input submitted or replayed.
- [ ] Repeat affected interactions on the final relationship-family checkpoint.
- [ ] Paired global installation and acceptance through codex.
- [ ] Review and installed delivery through integration owner.

Scratch owner: core-route-counts; purpose: source evidence and focused acceptance;
path: `/home/ts/.cache/agent-scratch/core-bar-sorting`. Retain concise results;
remove the downloaded paper and large generated output after delivery.
