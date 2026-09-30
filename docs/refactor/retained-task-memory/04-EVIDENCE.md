# Findings and source history

**Historical source:** `697bba42`. **Current integration:** `4295d680`.
Audit outputs and validation receipts are attached to
[PR428](https://github.com/OpenHCSDev/agent-comms/pull/428).

## Original promises

[PR48](https://github.com/OpenHCSDev/agent-comms/pull/48) supplied dormant contracts.
The [original plan](https://github.com/OpenHCSDev/agent-comms/blob/350b14a9a3ec2cf97d660a67b4835bd8c7619ea8/plans/adaptive-compaction.md?plain=1)
requires task boundaries, cache-preserving summaries, exact derived facts and
four-control retention evaluation. Acceptance includes three compactions,
corrections, failures, held-out questions and latency/usage/quality reporting.

`stack/adaptive-compaction-contracts.mjs:1-4` excludes Pi/ACP imports.
TriggerRule (`75-116`) takes supplied boundary/source references;
RetentionPolicy (`123-242`) selects supplied facts, tombstones and recent groups.
Their only importer is the prototype test. The missing production work is source
projection and task-aware selection. Supplied stamps cannot establish canonical
owner identity.

History: plan `908a02a5`, contracts `7e011d5d`, narrative revision fence `7df92ba0`,
trigger evidence `aab197df`, historical-status clarification `bedf6400`.

## Later delivery

| PR | Delivered mechanism | Remaining feature |
| --- | --- | --- |
| [40](https://github.com/OpenHCSDev/agent-comms/pull/40) | Bounded summaries, progress, empty-summary rejection | Task-boundary/exact-fact selection |
| [49](https://github.com/OpenHCSDev/agent-comms/pull/49) | Prevent discarded-history resurrection | Semantic recall |
| [70](https://github.com/OpenHCSDev/agent-comms/pull/70) | Bounded inbox with complete oversized payload | Relevant-fact retrieval |
| [95](https://github.com/OpenHCSDev/agent-comms/pull/95), [135](https://github.com/OpenHCSDev/agent-comms/pull/135) | Default journaled activation, continuation, effective settings, selected route | Task-aware trigger and retention policy |
| [150](https://github.com/OpenHCSDev/agent-comms/pull/150) | Remove arbitrary native reduction-call cap | Retention comparison |
| [349](https://github.com/OpenHCSDev/agent-comms/pull/349), [401](https://github.com/OpenHCSDev/agent-comms/pull/401) | Correlated progress/summary publication | Task-memory source projection |
| [412](https://github.com/OpenHCSDev/agent-comms/pull/412) | Scheduled goals use the journaled mechanism | Rubric-based timing |
| [416](https://github.com/OpenHCSDev/agent-comms/pull/416) | Shared retained/reasoning output accounting | Matched retention study |
| [421](https://github.com/OpenHCSDev/agent-comms/pull/421), [425](https://github.com/OpenHCSDev/agent-comms/pull/425) | Canonical state and turn lifecycle | Extend those original owners |
| [439](https://github.com/OpenHCSDev/agent-comms/pull/439) | Shared native compaction admission | Retention through that same source/custody contract |

The PR bodies document their respective tests and live results. No cancellation
rationale for the four remaining features appears in these records. Absence of
implementation cannot establish team intent.

## Determining source owners

| Source | Answer owned |
| --- | --- |
| `owner_compaction_adaptive.py:131-196` | Selected settings decision, admission and summary route |
| `native-compaction-selected-summary.mjs:28-46` | Stored-context/shouldCompact trigger |
| `owner_compaction_settings.py:23-43` | Effective settings and threshold observation |
| `native-compaction-policy.mjs` | Bounded packing, map/reduction and output budget |
| `native-compaction-source.mjs:48-73` | History/previous-summary source |
| `owner_compaction_prepare.py`, `compaction_source.py` | Revision witness and native preparation |
| `owner_compaction_commit.py` | Journaled commit and original-input admission |
| `owner_compaction_provider.py:21-70` | Summary outcome family |
| `goals.py`, `goal_attempts.py` | Goal revision, status and attempt |
| `turn_input_source.py`, `input_disposition.py` | Original input and disposition |
| `messages.py:46-156`, `wire_log.py:365-386` | Original sender, seq/ID, claim linkage and certified reference reads |
| `envelope_claim_transitions.py:216-289` | Original claim/release events and current claim projection |

Native package `native-current-776dc36857e630da` used HistorySummarySource,
policy-sized reduction/synthesis, `cacheRetention: "none"`, and a fresh routing ID
when none was supplied. S3 owns provider-route capability and measurements.
Matching request text or a cache-retention hint cannot establish a provider hit.
Saved checkpoint counts cannot identify their trigger; forks can inherit entries.

## Reproduce

```sh
rg -n 'TriggerRule|RetentionPolicy|rubric' src stack/adaptive-compaction-contracts.mjs
rg -n 'requiresCompaction|shouldCompact' stack/native-compaction-selected-summary.mjs
rg -n 'class |def |require_current' src/agent_comms/owner_compaction_{prepare,commit,provider}.py
python -m unittest discover -s tests -p test_compaction_retention_fixture.py -v
python tests/compaction_retention_fixture.py
```

The full contextual scaffold scan covered 292 source files, all 85 detectors,
zero omissions and nine indexed classes. The scorer repair removed internal raw
aggregate records and made totals/source identities derive from their outcomes.
S4 carries its behavior and serialization contract. The cache-root traceback is
tracked in [NRA issue 13](https://github.com/OpenHCSDev/NominalRefactorAdvisor/issues/13).
