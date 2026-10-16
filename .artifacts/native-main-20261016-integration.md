# Native/main integration, 2026-10-16

Owners: core-native-runtime; Core #1007, P18/P40/P41/P62.

Sources: native f6422df046aca0383f5330bcba5fbbf32808cc01 and main 768e03d44cc36058cb5d0fd253cb4e8d3954f53e. Automatic checkpoint e458517b8 recorded the unfinished merge, including markers; the following repair removes them. History is preserved, not rewritten.

- ConfigOptions(comms) remains the only construction path. Delete main's configured() factory, raw binary/arguments copies and ACP catalog declarations; selected Comms supplies runtime/settings/package. Configure, New/Fork validation and prepared fields share this owner. Field preparation keeps explicit Comms because a process-default factory cannot discover a different selected installation correctly. claude-session informed.
- Preserve main's create(spec), spec.task first input, source-only title and registered-session-model changes. Keep explicit recorded caller identity rather than main's raw --from caller fields. CliOption remains the one option owner, including late settings omission resolution.
- Restart queue target selection remains RestartTarget(selection, native); QueuedRestart owns launch_runtime and runtime environment. enqueue derives selection from Comms; its tools and publication caller no longer pass a second runtime. Publication constructs Comms for its own installed target. This changes source only, not the installed pointer or restart queue.
- Before AST: both clean source snapshots, paired Toad production and actual metaclass dependency, 1865 modules, zero parse omissions. After: merged production/tests/tools, paired Toad and dependency, 1086 modules, zero parse omissions. Dynamic dispatch is not established by this census. Snapshots are disposable source copies recorded by e458517b8 and retired after checking active processes; original Git sources remain available.
- CLI imports and 55 command help projections regenerate from declarations. Focused source batch: 17 passed, 1 saved-native-selection case deselected. Includes typed default/explicit selection, command contract, caller boundary and registered session model. This is not configured-provider, installed fork, or UI acceptance.
- Known unfinished work remains: native fixture family migration (including old restart target fixtures), current exact private wheel, actual default/explicit fork rows and first reply, TRIAGE-to-FULL tools/nonempty reply, and P67 paired installed UI.

Resources: /home free 5.5 GiB, / free 4.3 GiB, available RAM 15.8 GiB, swap used 17 GiB. No SDK rebuild, dependency clone, broad suite or parallel agent launch. Any private Core wheel operation must remain small and reuse the existing runtime/dependencies.
