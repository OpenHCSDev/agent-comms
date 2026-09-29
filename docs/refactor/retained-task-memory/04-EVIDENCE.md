# Evidence and limits

## Source identities

- Repo audited: `OpenHCSDev/agent-comms`, fresh main
  `697bba42f5f03e169ff8eae9490090cbd0d0b89e` after normal integration of moving
  main. Initial investigation was at `350b14a9`; PR416/418/423 merged during
  preparation. The ten class-census modules have zero diff between these heads.
- NRA declaration-census source: local `nra-installed-main-20260928` worktree,
  `ab85aa0b0724894f81e16e9fda30fc290fc0aa27`. Interpreter: installed stack Python
  3.14. That initial census had no dependency detector scan, raw-findings corpus
  or migration proof. The later complete contextual scaffold scan and raw leads
  are recorded in [05-AUDIT-REVIEW.md](05-AUDIT-REVIEW.md).
- Installed-path observation: worker runtime `runtime-journaled-original-20260929`
  and native package `native-current-776dc36857e630da`. Read-only environment and
  installed-source observation established the selected journaled path exists and
  defaults enabled. This is not proof all original features are live or a fresh
  behavioral acceptance of that deployment.
- A session checkpoint count alone proves saved compaction entries, not which
  trigger produced each entry. Forked histories may include inherited checkpoints.
  No private session content or paths are needed in this PR.

## What the original proposal actually said

[PR48](https://github.com/OpenHCSDev/agent-comms/pull/48) is explicitly a dormant
prototype. [Historical plan](https://github.com/OpenHCSDev/agent-comms/blob/350b14a9a3ec2cf97d660a67b4835bd8c7619ea8/plans/adaptive-compaction.md?plain=1) sections
Implementation sequence 2, 3, 4, 5 specify task-specific boundaries,
cache-preserving summaries, exact derived task facts and retention evaluations.
Its acceptance gates require three compactions, corrections, failures, held-out
questions, four comparison controls and latency/usage/quality reporting.

`stack/adaptive-compaction-contracts.mjs:1-4` explicitly says it is not imported
by Pi or ACP. `TriggerRule:75-116` needs caller-supplied source and boundary
references; `RetentionPolicy:123-242` selects owner-provided facts, tombstones and
recent groups. Its comments explicitly warn that those stamps are not canonical
owner proof. These are working provider-free contracts, not a live fact extractor
or task-boundary rubric. Search at this head found no production integration of
those class names. Reference:
[contracts](../../../stack/adaptive-compaction-contracts.mjs).

Local history records the plan at `908a02a5`, contracts at `7e011d5d`, narrative
revision fencing at `7df92ba0`, trigger evidence at `aab197df`, and historical
status clarification at `bedf6400`. None of those observations licenses silently
copying the dormant object shapes into production.

## Later delivery and its scope

| PR | Recorded delivery | What it does not establish |
| --- | --- | --- |
| [40](https://github.com/OpenHCSDev/agent-comms/pull/40) | Bounded summaries, progress, empty-summary rejection, real/native regression | Task-boundary or exact-fact policy |
| [49](https://github.com/OpenHCSDev/agent-comms/pull/49) | Prevent discarded history resurrecting on later tool rounds | Better semantic recall |
| [70](https://github.com/OpenHCSDev/agent-comms/pull/70) | Bounded inbox presentation with complete oversized payload preserved outside inline context | Automatic retrieval of relevant facts |
| [95](https://github.com/OpenHCSDev/agent-comms/pull/95), [135](https://github.com/OpenHCSDev/agent-comms/pull/135) | Default activation, continued sessions, effective settings, selected route | Activation of dormant TriggerRule/RetentionPolicy |
| [150](https://github.com/OpenHCSDev/agent-comms/pull/150) | Remove arbitrary four-call cap on native chunk/reduction plan | Retention-quality comparison |
| [349](https://github.com/OpenHCSDev/agent-comms/pull/349), [401](https://github.com/OpenHCSDev/agent-comms/pull/401) | Correlated progress and summary publication | Task-memory source authority |
| [412](https://github.com/OpenHCSDev/agent-comms/pull/412) | Scheduled goal inputs use existing journaled mechanism | Rubric-based timing |
| [416](https://github.com/OpenHCSDev/agent-comms/pull/416) | MERGED shared retained-versus-reasoning output accounting; integrated into this branch | Comparative retention study or fresh live acceptance by this draft |

PR-body test/live claims above are attributed reports, not independent reruns in
this draft. GitHub open/merged state was queried at publication preparation.
No cancellation/rationale for abandoning the remaining features was found in
these documents. Do not infer team intent from absence of implementation.

## Fresh live-path trace at the audited source

| Witness | Determining fact or operation |
| --- | --- |
| `src/agent_comms/owned_turn.py:310-375` | Prepares selected owner and pending original before compaction |
| `src/agent_comms/owner_compaction_adaptive.py:136-158,184-248` | Reads selected settings decision; skips on no trigger; routes same selected summary and commit |
| `stack/native-compaction-selected-summary.mjs:28-46` | Trigger is stored-context requirement or `shouldCompact(tokens, window, settings)`, not a subtask rubric |
| `src/agent_comms/owner_compaction_settings.py:23-43` | Typed effective-setting observation, no task-boundary fields |
| `stack/native-compaction-policy.mjs:7-19,39-85` | Shared strategy/configuration/packing/output owner, including merged retained-output accounting |
| `stack/native-compaction-source.mjs:40-61` | HistorySummarySource serializes history/previous summary, not a canonical task-fact projection |
| `src/agent_comms/owner_compaction_prepare.py:30-104` | Native witness, preparation result family and typed helper |
| `src/agent_comms/owner_compaction_commit.py:43-94,118-209` | Source capture, journaled commit, and original-input admission with owner/source rechecks |
| `src/agent_comms/compaction_source.py:16-59` | Revision-bound source contract, not another fact database |
| `src/agent_comms/owner_compaction_provider.py:21-70` | Existing outcome family; leaf outcome behavior already owned |
| `src/agent_comms/goals.py:70-120`, `goal_attempts.py:496+` | Goal/revision/attempt sources, independently persisted |
| `src/agent_comms/turn_input_source.py:97-187`, `input_disposition.py:144-229` | Original-input family and disposition owner |

The prepared native `compaction.js` inspected in native776 uses
`HistorySummarySource`, policy-sized map/reduction and final synthesis. Its
`completeSummarization:347-364` sets `cacheRetention: "none"` and uses a fresh
routing ID when none is supplied. This is an adapter request hint, not proof of
provider cache behavior. It is different from appending a probe/summary instruction
to the original prefix. Cache-preserving mode was not found in this selected path.
Other transports must be traced before a universal unsupported-capability claim.

## Bounded class-first evidence

[class-census.txt](evidence/class-census.txt) records every original ClassDef in
ten selected Python modules with original syntax ordinal and whether uniquely
joined to NRA's canonical class-family index. It uses existing
`parse_python_module_roots(... use_parse_cache=False, parse_workers=1)`,
`module_syntax_index(...).indexed_nodes_of_type(ast.ClassDef)` and
`build_class_family_index`. The first exploratory command failed because it
incorrectly treated `(ordinal, node)` rows as nodes; corrected iteration produced
the saved census. No changed production source or fabricated indexed owner.

This census does not include all dependent modules, native JavaScript classes,
or a complete NRA detector scan. The native776 compaction.js inspected read-only
had SHA256 `33f969302e89cc99064a04bce76ba97d5be67466b53af27dce1ac1fb0b90e3f7`;
this is an observation of that installed version, not the newly merged native
package's content. Therefore the receipts propose determining
owners with explicit OPEN binding/effect/consumer obligations rather than
claiming an admitted implementation-consumer relation or equivalent DSL plan.

## Reproduce the feature gap without a provider

```sh
rg -n 'TriggerRule|RetentionPolicy|rubric' src stack/adaptive-compaction-contracts.mjs
rg -n 'requiresCompaction|shouldCompact' stack/native-compaction-selected-summary.mjs
rg -n 'class |def |require_current' src/agent_comms/owner_compaction_{prepare,commit,provider}.py
python -m unittest discover -s tests -p test_compaction_retention_fixture.py -v
python tests/compaction_retention_fixture.py
```

Search evidence is scoped to the recorded corpus, not a runtime reachability
proof. The new scorer proves its measurement semantics; native/source-storage
behavior and model retention still require S4's separate acceptance runs.

## Validation of this draft

- Eight provider-free unittest checks passed, including the new-question
  maintenance experiment, stale corrections, missing answers, malformed/duplicate
  input and the actual exporter subprocess.
- Actual CLI scoring smoke accepted 21/21 authored oracle answers, with zero
  stale answers. This tests scoring, not a model. The small answers file was owned
  by this thread in `/home/ts/.cache/agent-scratch/comms-retention-scorer-owner-*`
  and removed on exit; no session/provider output was copied.
- Black check passed for both new Python files. Plan-package links, leak review,
  surface consistency and style checks passed. The first package check could not
  resolve a valid link outside its flat document set; the historical source now
  uses a pinned GitHub link. It was not a missing source file.
- `git diff --check` passed before staging; the cached diff is checked before
  commit. Production source changes: zero. Full suite/native/model-quality
  acceptance: not run by this draft.

The lack of deletions is intentional: this requested evidence/evaluation PR does
not replace a production mechanism. Surface implementations must delete displaced
contracts and migrate all consumers as specified in their receipts.
