# P125: preparation entry-point group replay

NRA PR #348, head `f9f5ba3ad0b88c41d58c5d6c7e1e6f7da6e49df1`, merged as `70296d7b124e712d1f00ea27d807438b9578f2f3`. Exact-head source approval: teammate message `0ef95582cda2`.

The same four-input `ReplayGitModuleImportsOperation` now reads caller manifests as move inputs, including metadata-only callers. The existing manifest editor owns physical edits; the historical policy admits only matching PEP 621 group-key spans and only caller-governing manifests.

Git's rename alone does not prove a group rename. Both explicit provider `entry_point_group` literals must match their old/new module identities, and the provider class shape apart from that declaration must agree. Changed provider behavior and unsupported key spellings refuse. Unrelated values, group keys and hook targets stay untouched.

## Original control

`original-group.json` records Core `311cc7bb47dede8edbea6e0175bd4a1ee6a8d553` → `00ff77ae91cb8dea6c788000db3525219be73c5f` and Toad `c19b927039f8811eb466a534e65c3dd634d1879f`.

Only this header changes:

```diff
-[project.entry-points."agent_comms.runtime_preparation"]
+[project.entry-points."agent_comms.installer.runtime_preparation"]
```

`neovim = "toad.neovim_runtime:Neovim"` is byte-for-byte unchanged. The target-group selection changes from 0 hooks to 1. No app modules were imported and no Python source changed. `run_original_group.py` retains the exact private native run; this was not a full historical source scan.

`metadata-focused-2.log`: 12 passed in 6.76s, exit 0. Covers metadata-only replay, unchanged hook and unrelated strings, provider-behavior refusal, existing Python replay, and three existing manifest-owner controls. The preceding failed run was a missing fixture parent directory, retained in `metadata-focused.log`.

The API is unchanged. Preparation remains mandatory. Friction owns wheel rebuild, recovery/carry and the live installation; none is claimed here.
