# P115: whole-script placement

NRA PR: https://github.com/OpenHCSDev/NominalRefactorAdvisor/pull/345
Final head: `85cb3a2ee099d0f0c3fde4494d085d79ff5f9b05`.

## Original job

`whole-script-placement.plan.json` moves the original `command_target_probe.py` and `goal_read_probe.py` from `tests/native` to `tests/support` through the native `relocate_module_to_new_module` operation. Their bodies, cold-import order, assertions, argv reads and JSON output stay unchanged. This supersedes the native capability gap in the historical `toad-guard-followup.md`; no surrogate declaration or wrapper was introduced.

The shared-pair projection is clean and changes exactly the two original paths and two destination paths. Receipt: `p115-original-script-simulation.json`. The final projection is pinned to clean committed head `85cb3a2ee099d0f0c3fde4494d085d79ff5f9b05` and changes exactly four script paths.

## Real consumer

`run_original_probe_placement.py` copied the original pinned scripts into a private fixture under this allocated checkout, applied the native operation there once, and ran each script before and after against the actual P23 production classes in the existing environment. It did not edit either shared checkout.

- Core: `d00155dd3d456025df7b3f4c56253fe9a5115a2b`.
- Toad: `c0020780a3f8a67550575d193eb8fa9222241b9d`.
- NRA production code: `2cafc6bad7f457bac3fa6b6ba2877ac7cc36c77a`.
- Both scripts: before and after exit 0, empty stderr, identical JSON.
- Both original source SHA256s unchanged: `2aac65303799060a2989b7d027e2f3a76b8082a56ba0a0d67706f1046c050629`, `8d028a84d4e8e0090a1ec6de8095ea5213939941d32782b3de49da791b2a5a26`.
- All three source heads stayed unchanged throughout the consumer attempt.

Full receipt: `p115-original-probe-consumer.json`, SHA256 `6e547cc9e703a517f8562c7e6b4b833f6a856176c05142812eecef27c7b38152`.

## Ownership and proof boundary

`NewModuleCreationOperationABC` owns atomic source creation and missing package declarations. Both the existing symbol moves and the new whole-module move inherit it. Whole-module placement composes that capability with `RetainedCallOperation`. The existing `RelocationNameRedirect`, `_ModuleIdentityStrings` and `_ModuleSiblingData` own consumer rewriting and supported location behavior; their implementation was not copied into a new mover.

The new operation checks location answers after the native planner has indexed its virtual destination. The first collected run exposed that phase ordering error: 4 failed, 46 adjacent cases passed. The phase fix then passed all four new controls. The retained adjacent results cover symbol/dissolve, annotation and repository guards; they were not repeated as another P23 gate. An actual importing caller exposed one more omitted fact: `ModuleSymbolTable.top_level_names` excludes imported names. Whole-module redirects now derive both declared and imported bindings. The final four controls pass in 6.41 seconds, executing real importing and runpy script-path callers before and after each script shape. The original-probe runtime receipt remains pinned to the predecessor; the final projection preserves the same bodies and paths.

Unproved cross-package relative imports and changed location-dependent reads refuse. The actual consumer evidence covers the two declared first-module inputs, not every arbitrary future Python program or environment. No new agent, Claude dependency, paid call, shared source edit or P23 installation was used.

## Integration

Friction retains P23 application and its paired install/real Standby acceptance. The independent NRA operation and its native plan are delivered here; this tooling work does not redefine P23's finish line. The separate unchanged-main bus-key and app-test placement plans remain in this directory.
