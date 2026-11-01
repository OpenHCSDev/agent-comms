# Lessons already paid for in OpenHCS

History, not instructions: no rule or instruction file points here. The behaviour these
PRs illustrate is owned by the commander rules (slop-and-polymorphism, correct-maintenance,
refactor-game) and the papers they derive from.

- #44: share widget lifecycle behavior through base classes and capability hooks.
- #45: derive registration and discovery from declarations and AutoRegisterMeta.
- #51: reuse widgets, target invalidation, resolve once and prepare off the UI thread.
- #58: ObjectState owns model/history/provenance; forms consume it. Paint derives
  animation colors from time instead of maintaining and updating per-element colors.
  LiveContextService was repeatedly edited before its 364-line deletion in
  `1a07ca5d63e9ca99923c0e909da9253dfb754ec3` on 2025-12-08.
- #60: `5e8812ee83d0dc8714392445bad3e32fc47a1755` deletes the competing compiler/runtime
  lattice; its AST deletion gates trace the replaced mechanism across consumers.

The full analyses are in the NRA repository's
`docs/source/development/openhcs_high_significance_pr_deep_dives.rst`,
`openhcs_june_pr60_calibration.rst`, `openhcs_diff_evolution_case_studies.rst`
and `openhcs_detour_case_studies.rst`.
