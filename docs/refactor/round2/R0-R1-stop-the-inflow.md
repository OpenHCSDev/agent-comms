> Current owner override: CI is deferred. Run the packaged ratchet and guards locally; the workflow is manual and no merge gate is enabled by TR0.

# R0 and R1: stop the inflow, and make it visible

**Heads:** `agent-comms` `15a4d00`, NRA `1119ca6`. **Rules:** [00-RULES.md](00-RULES.md). **Step 1**, colliding with nothing.

### T4 addition: independent class sizes

The same installed ratchet now derives `ClassSize` from its owning declaration.
Each existing class has its own lexical line-span delta, including decorators;
another class shrinking cannot cancel its growth. Full source inventories retain
unique qualified-name baselines across file moves and distinguish duplicate names
by their path. New owners report a null baseline until their first merge; deletions
report zero size. No fixed cap, allowlist, copied script or new required CI gate.
The workflow supplies current main as base. TR0 owns this addition; T4 decomposition
still follows its assigned surface ordering.

---

## R0: the required check

### What it runs

One fast workflow, `.github/workflows/debt-ratchet.yml`, on every pull request, which is **the required status check on `main`** (D18). It runs two things:

1. **The ratchet.** `agent-comms-ratchet --root src/agent_comms` sums three measures over the Python files under `src/agent_comms/` that the PR touches, at the PR's base and at its head, and **fails if the head total is higher:**
   - `type(x) is …` and `type(x) is not …` comparisons;
   - boolean chains of four or more operands;
   - subscripts with a string-literal key.

   Totals over touched files mean moving code between files nets to zero, so relocation and real refactors pass. New files count from zero; deleted files count as zero; renames are followed. There is no baseline file: base and head are compared directly.
2. **Every surface's guards.** Guard tests live in the suite, marked so the required check can run just them in seconds. Each surface adds its guards when it merges, so nothing a surface removed can return.

**There is no exception mechanism.** No pragma, no allow-list, no skip marker. If a shared-abstraction module genuinely needs one more raw access, the PR says so and the owner overrides that one check by hand, visibly.

### Why required and fast

The slow nine-job matrix stays asynchronous, as the system prompt says. This check takes seconds, so requiring it costs nothing. And a required check that a `[skip ci]` commit skips never reports, which keeps the PR unmergeable, so this also ends `[skip ci]` merges; confirm that on the first PR.

### Done when

- The script and workflow are merged, and the check is required on `main`.
- Four tests for the script: an added `type()` check fails; moving code between files passes; a reduction passes; a new file's smells count.
- Run over `a6b43fe` and the current head, it reports the totals this series has used.
- It finishes in under a minute on a real PR.

---

## R1: teach NRA the left rung

NRA reports 6 findings while hundreds of raw record reads go unseen, because its detectors key on declared families. R1 is built **inside NRA, on NRA's own machinery**: detectors subclass `IssueDetector`, registered with IDs derived from class names; `semantic_descent.py` already matches raw shapes (`MAPPING_LITERAL` and others) against classes that own a field set.

- **R1.a, mapping reads.** A new projection kind: the string keys a function reads from one subject by subscript or `.get`. The existing `semantic_mirror_without_descent` detector then reports reads that mirror a class's fields. **The decode site, where the keys feed that class's constructor, descends into the authority and is not a finding;** every other raw use is.
- **R1.b, `UnmodeledRecordShape`.** A new detector for key sets read three or more at a time that match no class, aggregated by key set.
- **R1.c, `RedundantTypeCheck`.** A new detector for `type()` or `isinstance` checks on attributes whose declared class already fixes their type, for annotated parameters and `self`.

### Done when

- Each part has positive and negative fixtures, including a decode site producing no finding.
- Calibrated on agent-comms at the current head, with counts reported beside this series' prototype numbers and differences explained.
- The scan stays complete, and its time grows by no more than a quarter.
- The skill's decision receipt reports bypassed and unmodeled shapes beside the implementation pairs it reports today.

---

## Dispatch

> **`refactor-r0`:** Build R0 per `docs/refactor/round2/R0-R1-stop-the-inflow.md`. Read `00-RULES.md` first. You touch only `tools/`, the new workflow and the script's tests. No exception mechanism of any kind.

> **To the agent that owns NRA:** Please build R1 per `docs/refactor/round2/R0-R1-stop-the-inflow.md` in the agent-comms repo, on `IssueDetector` and `semantic_descent.py`. Round 2's surfaces use your detectors as part of their gate.
