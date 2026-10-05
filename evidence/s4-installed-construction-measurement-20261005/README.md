# Installed construction measurement

The configured #674 runner calls `armConfiguredNativeCondition`, which records
`installed-transform-applied` through `armInstalledNativeCondition`. The old
`condition_application` reader instead required `bounded-transform-applied`,
produced by the separate direct post-transform summary replacement SDK control.
These operations are different. Their shared converter/request binding remains
`RecordedNativeProbe.condition_message_binding`.

`RecordedNativeProbe.bounded_summary_replacement` now names that separate
operation. Its original source, retirement and converter checks remain intact.
All measurement/scorer consumers use the explicit name. The installed request
continues to be read by the existing `RecordedConditionInstallation` family.

`ScoredScenario.condition_construction` derives availability from the observed
constructor agreeing with the declared arm and its complete message partition
being bound to the original request, for every frozen round. A measured changed
prefix stays changed; it is not successful preservation. Missing constructor,
partition or converter remains unavailable. Entry selection, narrative source,
full-history admission and capacity retain their own original evidence.
The paired view borrows these same results; study acceptance stays unavailable.

The original #674 paired report and its configured sources are immutable. Source
inspection found both installed prefixes measured/preserved (33 constructed
messages, 34 request messages), while entry-based full-history selection was
not captured. The task-memory constructor does not use the bounded arm's
narrative-only source. No new configured read, SDK, provider or holder run is
part of this checkpoint. This is a reader/scorer change, not retrospective
replacement of the original report or study acceptance.

## Source coverage

Existing refactor-audit `Package` parsed HEAD's 324 production, 373 test and
54 tool modules with zero omissions before editing. Declarations/calls and
measurement-key consumers were enumerated through its AST. The native private
mjs producer/caller family was read separately; it is not counted as Python AST
coverage, and dynamic dispatch is not established by lexical references.

The obsolete measurement method `applied_condition`, exported
`condition_application`, and grouped `bounded_sdk_application` are deleted
across their callers. The configured journey's independently named
`condition_application` coroutine is a real prompt resource and is unchanged.
Runtime, native stack, configuration and compiled artifacts are unchanged.

Final proportionate authored checks are pending; no configured or study claim.
