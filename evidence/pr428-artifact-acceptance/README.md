# #428 artifact acceptance

Owner: Mendel. Tested merged artifact: `17a43e0271cb661ee893d0ce6b34d2ae6ac6e5d2`.
This isolated test worktree preserves the original scorer, tests and plan bytes.
No production changes were made.

## Current installed boundary

Installed runtime: `/home/ts/.local/share/agent-comms/runtime-sidebar-native-custody-20260930`.
Its direct_url.json identifies Core `6feb634ba184d94d763028406152fff07027cccc`.
The default agent-comms launcher resolved to this same runtime before and after
acceptance. This is an artifact CLI check against the current installed dependency;
it does not assert a native/UI/model compaction journey.

Invoke that runtime's `bin/python`, without PYTHONPATH, using `-I`. The observer
asserted sys.prefix and the imported FieldCodec file belong to that runtime.
Scorer/test hashes and the installed FieldCodec hash all match the original
published #428 receipt. Preserve the prior ten-test, twenty baseline/candidate
CLI-pair and eleven invalid-CLI results; the matrix was not repeated.

## Newly executed outcomes

- Exact merged ten unittest controls: PASS, 0.449 seconds. The controls include
  all four condition labels, all eleven invalid decoder shapes, fixed denominators,
  identifier exactness, source-derived totals, new-question extension, unknown
  identities and an actual exporter subprocess.
- Twelve serial actual CLI invocations: PASS, 5.210135 seconds total.
- Four successful invocations: exporter; authored exact oracle 21/21;
  stale original answers 13/21 with eight stale facts (rounds 7/4/2 correct,
  0/3/5 stale); missing answers 0/21 with all 21 missing.
- Eight invalid invocations rejected with exit 1 and no score stdout:
  malformed JSON, duplicate round, duplicate question, boolean answer, null
  answer, array top-level, unknown round and unknown question.
- Exported history/questions matched the canonical scenario exactly: three
  rounds with seven questions each. Each exported question contains only id/prompt;
  expected/obsolete/evidence metadata is omitted. Recall facts remain in history.

The machine receipt records exact invocations, outcomes, error tails, hashes,
installed provenance and absolute raw-evidence paths. All bounded subprocesses
exited. Zero provider calls, native inputs, public inputs or public-root mutations.

## S4 measurement limits

Authored answers validate the scoring oracle; they are not measured model recall.
Condition names do not implement context construction. #428 adds plans and tests,
not S1-S4 production implementation.

Canonical S4 requires independently measured fact availability, prompt presence
and model recall; frozen probes, actual context digests, immutable implementation/
model/settings, original source and raw model answers; representative coding,
research and long-goal histories with at least three actual checkpoints; matched
controls, predeclared sample/spend and paired confidence/cost/latency margins.
It also requires zero unauthorized constraint/Decision revision mass and held-out
lock-in/action probes. Lost required items count as unauthorized revisions unless
sanctioned scope/lifetime ends; unrelated corrections cannot authorize revisions;
missing provenance fails closed and no eligible identities is N/A, not zero.
These real runtime/native/model gates remain pending, including S1 timing and
S2 memory production, S3 provider-cache proof, S4 runner and default-flip disposition.
The synthetic 21-question result cannot close those gates.

## Evidence custody

Raw CLI inputs/stdout/stderr, the one-time check script, complete original PR
receipt and installed provenance remain under
`/home/ts/.cache/agent-scratch/comms428-artifact-acceptance-20260930` (180 KiB before
this final receipt update). Retain them as acceptance evidence. No build, venv,
owner or background process was created. No active comms428 worktree was edited.
