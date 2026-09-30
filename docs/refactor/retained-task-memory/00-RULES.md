The round-2 rules (`docs/refactor/round2/00-RULES.md`) apply to this work; these rules add to them.

# Binding rules

## Ownership

Own each answer at the declaration that determines it. Put shared algorithms on
the meaningful public parent and irreducible behavior on its cases. Derive
consumers, names and discovery from those declarations. A new case must not need
synchronized edits to several interpreters, a helper switch or a parallel roster.

**No state duplication.** Derive from the authoritative source. Read-only views
and exports have no independent update lifecycle, shadow store or second cache.
Settings own budgets; goals and inputs own their state; native history owns its
entries; the wire owns authored messages and claims; transport owns cache support;
existing admission and commit owners retain execution authority. A summary or
model answer cannot finish a goal, clear UNKNOWN, grant execution or replay input.

Before code edits, run refactor-audit and NRA tools. Census original classes,
trace determining declarations and every caller, and record complete contextual
detector coverage and omissions. Matching field names alone cannot establish
identity between independently owned facts. Pattern IDs identify ownership risks.

## Boundaries

Decode external input once using existing FieldCodec and typed declarations.
Extend their capabilities rather than adding codecs, queues, journals, registries
or provider clients. Honor external Pi/provider/ACP/OS contracts. Change our
formats and callers together; delete displaced readers, aliases and dormant
contracts when their production replacement lands.

Record conditional/nested classes, aliases, dynamic binding and alternate callers.
Default to refusing an unsupported binding or effect until its source contract is
resolved. Inspect omitted detectors and raw-shape leads. Clean syntax and passing
scorer tests cannot establish runtime equivalence or model retention.

## Data protection and cutover

Use isolated persistent worktrees. Preserve the primary checkout, active bus,
saved sessions, journals, unresolved inputs and others' work. Use synthetic public
fixtures. Live resets, replay, paid evaluations and installation require explicit
authorization. Durable schema changes use one reviewed migration and operator
cutover; never reset journals or inputs. Replace rebuildable projections only
after their producers stop.

## Verification and delivery

Review directly with Python and the skill tools; do not delegate review. Tests
protect behavior and exact-state invalidation, rather than internal-format goldens.
Measure storage availability, prompt presence and model recall separately. Add a
new-case maintenance experiment and narrow guard with each ownership change;
finish every consumer before closing a surface.

Run bounded provider-free tests first, retaining errors and negative controls.
Real-model runs require a declared model/sample/spend budget. Report source,
native, model and deployment results separately. Matching text cannot establish a
provider cache hit. Publish useful verified checkpoints without waiting for CI;
respect enforced merge rules.
