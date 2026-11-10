# P122: repair importing callers from recorded module moves

Merged NRA #346 and its initializer-only hotfix #347. Corrected head: `9647ed7a4199e567f04fefe712a96e3dd7a09600`; corrected main merge: `cd544fde9b73bfcb5418d093e6c59b1eff404fe8`.

Public API:

```python
from nominal_refactor_advisor.codemod import ReplayGitModuleImportsOperation

operation = ReplayGitModuleImportsOperation(
    provider_repository=str(core_git),
    source_revision=recorded_core_commit,
    target_revision=target_core_commit,
    caller_root=str(private_toad),
)
```

DSL operation: `replay_git_module_imports`, with those same four fields. Input references are resolved to immutable Git commits. The provider sources are read-only proof inputs. Neither app is imported; only the isolated caller tree is written. Existing `Repository` and `RelocationNameRedirect` own Git decoding and import rewriting.

## Evidence

- `original-callers-2.json`: actual Core `311cc7bb47dede8edbea6e0175bd4a1ee6a8d553` to `00ff77ae91cb8dea6c788000db3525219be73c5f`, Toad `c19b927039f8811eb466a534e65c3dd634d1879f`. Native application repaired **95 files**, including all four original declared_choice callers. No app modules imported. Exit 0.
- That full source run preceded the final import-position/initializer refinements. Executable focused controls cover those deltas; it is not represented as a final-head full application run.
- `final-tests.log`: ten focused controls passed before the initializer correction.
- `repository-guards.log`: 71 passed, 8,687 deselected, exit 0 on the operation head.
- `hotfix-tests.log`: eleven focused controls passed after the correction, including actual initializer-only imports through both historical replay and the original whole-script mover.
- Independent source approval and two executable contracts on the corrected head: teammate message `9117657361ad`; reported exit 0 in 2.27s.

The missed initializer-only case retained a deleted module through the symbol-relocation parent. `WholeModuleNameRedirect` now owns destination-only initialization once; both historical replay and P115 inherit it. Partial symbol moves keep source-plus-destination behavior. Existing from-import positions stay fixed; historical star imports without an execution-order proof refuse.

## Remaining work belongs to the install

The installer owner has the stable API and corrected merge. The actual wheel/import/copy-carry check and friction's live install remain separate; this report does not claim that the host upgrade ran.

The original full run spent 70.756s eagerly constructing the unrelated class-family snapshot before import planning. Its phase log is `original-callers-2.err`. That is a separate measured NRA defect, not an installation gate. No second performance investigation or broad rerun was started.
