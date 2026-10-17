P127, closes #895.

## Repair

`PeerRelevance.grants()` already owns capability membership. `CoordinationSegment.capture` now asks for `AlwaysListed` and retains the existing available-view check. No name/status checks, second roster, cache or new policy. Awareness, relationships and partner-activity producers are untouched. This is MEMB-2: consume the existing capability instead of independently treating every available view as prompt inventory.

Production: **one file +2/-2, net 0**. Tests: one file **+9/-5, net +4**, adapting two existing tests to the explicitly changed peer-inventory contract while retaining recent-addressability, incarnation replacement and the parent binding. No new test cases.

## Saved-session measurement

No provider turn or new input. A completed saved session was copied owner-only; the same frozen 62-view observation and typed relationship inputs were decoded by the existing FieldCodec for both revisions. The original observation had no collaboration for claude-code, so it is retained separately; an actual explicit collaboration was established through `ThreadRelationships.edit` **only in an isolated private clone**, then frozen for both measurements. Production never reads these evidence files.

| Prompt | Coordination bytes before | After | Initial Core injection before/after |
|---|---:|---:|---:|
| `ping` | 585 | 447 | 589 / 451 |
| active collaboration with @toad-theme | 608 | 608 | 675 / 675 |

The bare parent entry disappears from coordination inventory; the parent binding remains. The collaborator remains AlwaysListed. The collaboration observation also contains the 626-byte Relationships and 611-byte Partner Activity contributors. Existing addressed-message awareness checks pass. This is the current reduced context path, not a claim that hundreds of peers still reached the native preparation caller after P104.

Fixture hash: `279968ca14d838a393e2a6c923d3368b62a6fae591a431346dead959f0a4edb0`.
Evidence: `/home/ts/.cache/agent-scratch/core-p127-context/{before-collaboration,after-integrated}.json`.

## Verification

- Current main integrated normally; integrated saved-session measurements reproduce the same bytes.
- Focused existing relevance/doctrine family: **13 passed**, including stopped contacts, goal contacts, related-message awareness and partner activity.
- Ruff and whitespace pass. Initial production ratchet exits 0; final integrated receipt is `.artifacts/p127/integrated-ratchet.json`.
- Broader source batch has five independently reproduced baseline failures: three calls to retired WireLog context APIs, an installed-only journey deliberately rejecting source imports, and an outdated AST reader roster expecting `owned_turn.py` despite its context_relations delegation. Baseline: 22 pass/5 fail. They are not weakened or relabelled as passing.

## Source evidence and limits

Before/after AST evidence uses the existing NRA audit parser for `turn_context.py`, `peer_relevance.py`, `relationships.py`, and `owned_turn.py`: four complete owning/calling production modules, zero parse omissions. Full Core consumer search finds the capture in `TurnContext.for_owner`, reached by inspection and native `OwnedTurn`; no competing inventory-selection path was added. A whole-root source-sites helper run timed out and produced no usable result; no complete dynamic-resolution claim is made. Receipts: `.artifacts/p127/{before-targeted,after-targeted}.json`.

New relevance members need only declare/inherit AlwaysListed; the consumer contains no member roster.
