# P108 / NRA #327 independent review

Verdict: accept the scoped class-field/module-binding repair.

Reviewed PR head: `292221294abeb537a20c0b563019aaf47160af2d`.
Confirmed merged commit: `b2457f0d2d5623813a452aeb7d3c57d74c469c93`.
Independent execution input: existing NRA checkout at `5981d592bd5297342ad8f1f129e6bfdd213906f0`. The three changed implementation modules and two changed test modules have no diff from the merge commit. This is a rerun on that later main, not a claim to have checked out the merge tree.
Interpreter: `/home/ts/.cache/toad/test-env/ace79f6007097c3e/bin/python`, Python 3.14.8, ABI `cpython-314-x86_64-linux-gnu`. The owner's InterpreterBuildProbe captured the invoked path, executable and loaded libpython identities/hashes after the run in `interpreter.json`; this is a post-run interpreter observation, not an installation claim.

## Independent evidence

- `pr.json` / `pr.exit`: GitHub reports MERGED with the exact head and merge above, exit 0.
- `merged.diff`: merge first-parent diff of all five changed files.
- `catalog_binding_reproducer.py`: copy of the original retained reproducer, kept here to avoid overwriting the original negative evidence.
- `reproducer.log` / `reproducer.exit`: class_field **1 binding / clean**, no_field_control **1 binding / clean**, exit 0.
- `consumer-tests.log` / `consumer-tests.exit`: **237 passed in 40.74 s, exit 0**, using the original author's 15-file consumer list, run independently with `-o addopts= -n 0 -o faulthandler_timeout=60` under `timeout -s INT 300`.
- Six included native extract-and-execute cases exercise moved and unmoved attributes through lambda, method, and class-body reads, including preservation of the annotated field.
- The included controls retain genuine module/global rebinding counts and refusal, ordered class reads, closure lookup, comprehension lookup and refusal for path-dependent class namespace reads.
- Two earlier collection failures are retained separately as `consumer-tests-config-failed.*` and `consumer-tests-hook-failed.*`. Neither collected or ran tests. The first retained configured xdist arguments with xdist disabled; the second exposed the repository conftest's mandatory xdist hook. The successful run loads xdist but selects zero workers.

## Source trace

`lexical_scopes.py:271-286`: `_resolve_name_binding` returns the determining frame from the same ordered scope lookup as the resolution. `_resolve_name` projects that result; it does not retain a competing read resolver.

`declaration_dependencies.py:518-537`: each actual read carries that frame on its original AST node; external candidates retain their existing filtering. Function and comprehension visitors preserve their existing frame and evaluation-order rules (`:545-553`, `:620-645`, `:728-760`).

`module_node_index.py:193-219`: static write ownership follows existing `ScopeBindingProjection` declarations, including global/nonlocal declarations. `binder_of` consumes the shared read decision and calls `require_known` before using it. Stores fall back to the write projection.

`module_node_index.py:225-249`: the index projects the shared lexical decisions and existing compile-time binding declarations rather than retaining its old local-name collector/global table/read walk.

`refactors/module_move.py:1497-1503`: the move still checks real module rebinding and then consumes `module_scope_names`; `:1593-1598` removes an import only when all its module reads were rewritten. The change fixes the false class-field binding rather than weakening those checks.

Slop removed: IMPL-12, the independently reconstructed read-resolution procedure in ModuleNodeIndex. Its determining owner is LexicalScopeContext. Paper1 Theorem 4.7 addresses copies; Theorem 4.8 requires preserving distinct read/write meanings.

## Recording attempts

The exact-head review tool refused because #327 is already merged (`src/agent_comms/projects/head_review.py:868` requires an open PR). The first order-completion call refused the missing interpreter observation. The post-run native observation above supplies that missing evidence; these refusals are not product-source defects or failed tests. The second order-completion call succeeded: Core records P108 revision 26, order `e674dfd0a3c4` as `delivered`, with `checked_commit` on main and zero strikes. The problem row remains open; no installed/closed state is asserted.

## Scope

No product, test, engine or environment edits were made by this reviewer. No install, performance, complete P23 integration, corrected channel runtime qualification or Pi-closure completion is claimed. P23's eight Pi guarded-import refusals and local-import obligations remain open.
