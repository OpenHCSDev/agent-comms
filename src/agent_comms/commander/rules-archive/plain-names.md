---
name: plain-names
description: "User rule — name things by what they do in plain terms; never extend Codex-era jargon (custody, admission, receipt, original...); rename on touch"
type: feedback
---

The user said on 2026-10-10: "Name things by what they do in plain terms. Do not extend the previous agent's vocabulary (custody, admission, receipt, original): when you touch code that uses it, rename to a plain name in the same change."

**Why:** Codex-era jargon layers private names over concepts that already have plain names, so every reader has to translate. A name that disagrees with what the thing does is identity slop (IDEN-4) in .

**How to apply:**
- In code, PR text, commit messages and plans, use plain names.
- Words to avoid: custody, admission/admitted, receipt, original, qualified, canonical, and authority/owner used as filler.
- When touching code that uses them, rename with all callers in the same change, keeping the meaning exact. For example, receipt → checksums record, admitted → validated, original path → source path.
- This is rule 1b in `docs/refactor/consolidation-20261009/00-RULES.md` (#1172) and is included in `.claude/verify/DISPATCH.md`.

Related: the user values the OOPSLA paper as a calibration tool, because it makes "use native mechanisms" a checkable standard instead of an argument with agents.
