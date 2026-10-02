# Lessons already paid for in OpenHCS

Tristan's 2026-10-02 instruction: learn from the actual architectural corrections;
do not turn this history into another reporting or tooling project.

- #44: share widget lifecycle behavior through base classes and capability hooks.
- #45: derive registration and discovery from declarations and AutoRegisterMeta.
- #51: reuse widgets, target invalidation, resolve once and prepare off the UI thread.
- #58: ObjectState owns model/history/provenance; forms consume it. Paint derives
  animation colors from time instead of maintaining and updating per-element colors.
  LiveContextService was repeatedly edited before its 364-line deletion in
  `1a07ca5d63e9ca99923c0e909da9253dfb754ec3` on 2025-12-08.
- #60: `5e8812ee83d0dc8714392445bad3e32fc47a1755` deletes the competing compiler/runtime
  lattice; its AST deletion gates trace the replaced mechanism across consumers.

The authoritative analyses are in the NRA repository's
`docs/source/development/openhcs_high_significance_pr_deep_dives.rst`,
`openhcs_june_pr60_calibration.rst`, `openhcs_diff_evolution_case_studies.rst`
and `openhcs_detour_case_studies.rst`. Read the source relationships they identify.

Progress is making the correct ownership decision and implementing it throughout
the family. A workaround leaves the competing decision somewhere else. Use existing
Python machinery to remove that decision: metaclasses and subclass initialization
for declaration-derived discovery; context managers for resource lifetime; shared
base behavior, hooks and capability mixins for orchestration. Multiple inheritance
composes capabilities through C3 MRO; inheritance and composition are not opposites.
Decompose a large class when a real behavior can have an owner and be shared. An
abstraction is load bearing when consumers rely on its behavior and the old decisions
are deleted. Tests and the actual installed path confirm the implementation last.
