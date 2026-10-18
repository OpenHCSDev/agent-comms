## P91: tooling only; no product-growth credit for #1061

`tools/debt_ratchet.py` replaces and deletes `src/agent_comms/debt_ratchet.py`. Remove the product console entrypoint, move its sole audit dependency to the existing dev extra, and migrate every active import/CLI caller. Historical reports retain their original command names. Both commits declare `Src-Budget: 0`.

`ChangedSource` owns cached tokens and its tree. Vocabulary counting consumes that object. The shared full-span checker remains on `CleanChangedLines`; vocabulary's override is genuinely a different identifier range. The checker itself shrinks one line; the module move is relocation, not factoring.

### SourceLines owns rename-aware counting
Read Git's NUL-framed numstat with **50% rename detection across the whole repository**, then select entries touching the configured Python source boundary. A cross-boundary rename contributes its edited lines, not the old file's whole length. Delete the redundant blob snapshot and budget wrapper. No new measures, exemptions or budget increases. The report's SourceLines base/head now mean removed-lines-plus-budget / added-lines; delta is net edited lines.

Real-history CLI proof, pinned audit120:
- `eed21e60c^1..eed21e60c`: moved checker is **+14/-15, net -1** (not the estimated -29). Including the unchanged-size shared import, SourceLines base16/head15/delta-1, **exit0**.
- integrated main73..eed: SourceLines **+187**, **exit1**. The old move can no longer conceal the +188 product change.
- this PR against main00be560e8: SourceLines base28/head27/delta-1, **exit0**.

### Main-based sizes
- src: **+0 / -948**
- tools: **+947 / -0**
- tests: **+18 / -19**

#1061 must independently reach net0 outside the old checker; none of this relocation counts toward that target.

Before the rename-policy extension, **92 guard checks passed** and the cache-only refactor preserved byte-identical historical ratchet stdout on948^1..948 with the same declared dependency. Those are not a claim that the intentionally changed SourceLines base/head counters remain byte-identical. Current rename behavior is verified through the three real-history CLI calls above. No native/UI behavior, shared pointer, saved session or UNKNOWN input was changed. No CI requested.

Retained source/census/results: `.scratch/p71/{p91-before-pinned.json,p91-after-pinned.json,p91-final-guards.txt,rename-eed21e60c.json,rename-HEAD.json,p91-after-p93-before.json}` in the owning checkout.
